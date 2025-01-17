import argparse  # noqa: I001
import logging
from pathlib import Path
from typing import Literal

import torch
from diffusers import StableDiffusionPipeline, StableDiffusionXLPipeline

# fmt: off
# HACK(ryand): Import order matters, because invokeai contains circular imports.
from invokeai.backend.model_manager import BaseModelType
from invokeai.backend.lora import LoRAModelRaw
from invokeai.backend.model_patcher import ModelPatcher
# fmt: on

from invoke_training._shared.stable_diffusion.model_loading_utils import PipelineVersionEnum, load_pipeline


# TODO(ryand): Consolidate multiple implementations of this function across the project.
def str_to_dtype(dtype_str: Literal["float32", "float16", "bfloat16"]):
    if dtype_str == "float23":
        return torch.float32
    elif dtype_str == "float16":
        return torch.float16
    elif dtype_str == "bfloat16":
        return torch.bfloat16
    else:
        raise ValueError(f"Unexpected dtype: {dtype_str}")


def to_invokeai_base_model_type(base_model_type: PipelineVersionEnum):
    if base_model_type == PipelineVersionEnum.SD:
        return BaseModelType.StableDiffusion1
    elif base_model_type == PipelineVersionEnum.SDXL:
        return BaseModelType.StableDiffusionXL
    else:
        raise ValueError(f"Unexpected base_model_type: {base_model_type}")


@torch.no_grad()
def apply_lora_model_to_base_model(
    base_model: torch.nn.Module,
    lora: LoRAModelRaw,
    lora_weight: float,
    prefix: str,
):
    """Apply a LoRAModelRaw model to a base model.

    This implementation is based on:
    https://github.com/invoke-ai/InvokeAI/blob/df91d1b8497e95c9520fb2f46522384220429011/invokeai/backend/model_patcher.py#L105

    This function is simplified relative to the original implementation, because it does not need to support unpatching.

    Args:
        base_model (torch.nn.Module): The base model to patch.
        loras (list[tuple[LoRAModelRaw, float]]): The LoRA models to apply, with their associated weights.
        prefix (str): The prefix of the LoRA layers to apply to this base_model.
    """
    for layer_key, layer in lora.layers.items():
        if not layer_key.startswith(prefix):
            continue

        module_key, module = ModelPatcher._resolve_lora_key(base_model, layer_key, prefix)

        # All of the LoRA weight calculations will be done on the same device as the module weight.
        device = module.weight.device
        dtype = module.weight.dtype

        layer_scale = layer.alpha / layer.rank if (layer.alpha and layer.rank) else 1.0

        # We intentionally move to the target device first, then cast. Experimentally, this was found to
        # be significantly faster for 16-bit CPU tensors being moved to a CUDA device than doing the
        # same thing in a single call to '.to(...)'.
        layer.to(device=device)
        layer.to(dtype=torch.float32)
        layer_weight = layer.get_weight(module.weight) * (lora_weight * layer_scale)
        layer.to(device=torch.device("cpu"))

        if module.weight.shape != layer_weight.shape:
            assert hasattr(layer_weight, "reshape")
            layer_weight = layer_weight.reshape(module.weight.shape)

        module.weight += layer_weight.to(dtype=dtype)


@torch.no_grad()
def merge_lora_into_sd_model(
    logger: logging.Logger,
    base_model: str,
    base_model_variant: str | None,
    base_model_type: PipelineVersionEnum,
    lora_models: list[tuple[str, float]],
    output: str,
    save_dtype: str,
):
    pipeline: StableDiffusionXLPipeline | StableDiffusionPipeline = load_pipeline(
        logger=logger, model_name_or_path=base_model, pipeline_version=base_model_type, variant=base_model_variant
    )
    save_dtype = str_to_dtype(save_dtype)

    logger.info(f"Loaded base model: '{base_model}'.")

    pipeline.to(save_dtype)

    models: list[torch.nn.Module] = []
    lora_prefixes: list[str] = []
    if isinstance(pipeline, StableDiffusionPipeline):
        models = [pipeline.unet, pipeline.text_encoder]
        lora_prefixes = ["lora_unet_", "lora_te_"]
    elif isinstance(pipeline, StableDiffusionXLPipeline):
        models = [pipeline.unet, pipeline.text_encoder, pipeline.text_encoder_2]
        lora_prefixes = ["lora_unet_", "lora_te1_", "lora_te2_"]
    else:
        raise ValueError(f"Unexpected pipeline type: {type(pipeline)}")

    for lora_model_path, lora_model_weight in lora_models:
        lora_model = LoRAModelRaw.from_checkpoint(
            file_path=lora_model_path,
            device=pipeline.device,
            dtype=save_dtype,
            base_model=to_invokeai_base_model_type(base_model_type),
        )
        for model, lora_prefix in zip(models, lora_prefixes, strict=True):
            apply_lora_model_to_base_model(
                base_model=model, lora=lora_model, lora_weight=lora_model_weight, prefix=lora_prefix
            )
        logger.info(f"Applied LoRA model '{lora_model_path}' with weight {lora_model_weight}.")

    output_path = Path(output)
    output_path.mkdir(parents=True)

    # TODO(ryand): Should we keep the base model variant? This is clearly a flawed assumption.
    pipeline.save_pretrained(output_path, variant=base_model_variant)
    logger.info(f"Saved merged model to '{output_path}'.")


def parse_lora_model_arg(lora_model_arg: str) -> tuple[str, float]:
    """Parse a --lora-model argument into a tuple of the model path and weight."""
    parts = lora_model_arg.split(":")
    if len(parts) == 1:
        return parts[0], 1.0
    elif len(parts) == 2:
        return parts[0], float(parts[1])
    else:
        raise ValueError(f"Unexpected format for --lora-model arg: '{lora_model_arg}'.")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--base-model",
        type=str,
        help="The base model to merge LoRAs into. Both Hugging Face names and local paths are supported.",
        required=True,
    )
    parser.add_argument(
        "--base-model-variant",
        type=str,
        default=None,
        help="The Hugging Face Hub variant of the base model (E.g. 'fp16'). Optional.",
    )
    # TODO(ryand): Auto-detect the base-model-type.
    parser.add_argument(
        "--base-model-type",
        type=str,
        choices=["SD", "SDXL"],
        help="The type of the base model ['SD', 'SDXL'].",
    )
    parser.add_argument(
        "--lora-model",
        type=str,
        nargs="+",
        help="The path(s) to one or more LoRA models to merge into the base model. Model weights can be appended to "
        "the path, separated by a colon (':'). E.g. 'path/to/lora_model:0.5'. The weight is optional and defaults to "
        "1.0.",
        required=True,
    )
    parser.add_argument(
        "--output",
        type=str,
        help="The path to an output directory where the merged model will be saved (in diffusers format).",
    )
    parser.add_argument(
        "--save-dtype",
        type=str,
        default="float16",
        choices=["float32", "float16", "bfloat16"],
        help="The dtype to save the model as.",
    )

    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO)
    logger = logging.getLogger(__name__)
    merge_lora_into_sd_model(
        logger=logger,
        base_model=args.base_model,
        base_model_variant=args.base_model_variant,
        base_model_type=PipelineVersionEnum(args.base_model_type),
        lora_models=[parse_lora_model_arg(arg) for arg in args.lora_model],
        output=args.output,
        save_dtype=args.save_dtype,
    )


if __name__ == "__main__":
    main()
