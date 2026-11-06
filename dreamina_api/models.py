"""Observed Dreamina model configuration and request validation (no network I/O).

Configuration is a packaged, credential-free snapshot of the page on 2026-10-25.
It describes advertised options, not a promise that every combination was tested.
"""

from __future__ import annotations

from copy import deepcopy
import json
import math
from pathlib import Path


_CONFIG = json.loads((Path(__file__).parent / "data" / "model-config.json").read_text(encoding="utf-8"))
_MODELS = {model["id"]: model for model in _CONFIG["models"]}
_ALIASES = {value: key for key, model in _MODELS.items() for value in (key, model["model_req_key"], model["name"])}
DEFAULT_MODEL = "seedance-2.0-mini"
DRAFT_VERSION = _CONFIG["draft_version"]


def get_models() -> list[dict]:
    """Return independent public configuration dictionaries for the four models."""
    result = []
    for source in _MODELS.values():
        model = deepcopy(source)
        model["modes"] = ["omni", "frames"]
        model["configuration_status"] = "observed-page-config"
        model["configuration_date"] = _CONFIG["captured_on"]
        model["experimental_resolutions"] = [r for r in model["resolutions"] if r.lower() in {"2k", "4k"}]
        model["live_verification"] = (
            "Only the Mini 4s/720p/16:9 text-only request is used as the HAR protocol baseline; "
            "this metadata does not certify successful generation for other combinations."
        )
        result.append(model)
    return result


def model_config(model: str) -> dict:
    """Resolve a documented alias, upstream model key, or exact display name."""
    if not isinstance(model, str) or model not in _ALIASES:
        raise ValueError("Unknown model; choose " + ", ".join(_MODELS))
    return deepcopy(_MODELS[_ALIASES[model]])


def _number(value, label: str, *, positive: bool = True) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{label} must be a finite number")
    if positive and value <= 0:
        raise ValueError(f"{label} must be positive")
    return value


def _resource(value: dict, label: str, expected_type: str | None = None) -> dict:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an uploaded image/video resource object")
    resource = deepcopy(value)
    kind = resource.get("material_type") or resource.get("type")
    if isinstance(kind, str) and kind in {"image", "video"} and f"{kind}_info" in resource:
        info = resource[f"{kind}_info"]
        if not isinstance(info, dict):
            raise ValueError(f"{label}.{kind}_info must be an object")
        resource = deepcopy(info)
    kind = kind or resource.get("type")
    if not kind:
        kind = "image" if resource.get("image_uri") or resource.get("uri") else "video" if resource.get("vid") else None
    if not isinstance(kind, str) or kind not in {"image", "video"} or expected_type and kind != expected_type:
        raise ValueError(f"{label} must be a {expected_type or 'image/video'} resource")
    resource["type"] = kind
    for key in ("width", "height"):
        dimension = _number(resource.get(key), f"{label}.{key}")
        if not isinstance(dimension, int):
            raise ValueError(f"{label}.{key} must be an integer pixel dimension")
    if kind == "image":
        uri = resource.get("image_uri") or resource.get("uri")
        if not isinstance(uri, str) or not uri.strip():
            raise ValueError(f"{label} requires an uploaded image_uri (not an image URL)")
        if uri.startswith(("http://", "https://", "data:")):
            raise ValueError(f"{label}.image_uri must be an uploaded resource URI; upload URLs first")
        if resource.get("image_uri") and resource.get("uri") and resource["image_uri"] != resource["uri"]:
            raise ValueError(f"{label}.image_uri and uri disagree")
        resource["image_uri"] = resource["uri"] = uri
    else:
        if not isinstance(resource.get("vid"), str) or not resource["vid"].strip():
            raise ValueError(f"{label}.vid must be a non-empty uploaded video ID")
        _number(resource.get("duration"), f"{label}.duration (milliseconds)")
        _number(resource.get("fps"), f"{label}.fps")
    if resource.get("format") is not None:
        if not isinstance(resource["format"], str):
            raise ValueError(f"{label}.format must be a string")
        resource["format"] = resource["format"].lower().lstrip(".")
        if resource["format"] == "jpg":
            resource["format"] = "jpeg"
    if "file_size_bytes" in resource:
        _number(resource["file_size_bytes"], f"{label}.file_size_bytes")
    return resource


def _check_material(resource: dict, limits: dict, label: str) -> list[str]:
    warnings = []
    for field in ("width", "height", "fps"):
        if field not in resource:
            continue
        for prefix, comparison in (("min", lambda a, b: a < b), ("max", lambda a, b: a > b)):
            threshold = limits.get(f"{prefix}_{field}")
            if threshold is not None and comparison(resource[field], threshold):
                raise ValueError(f"{label}.{field} violates configured {prefix}_{field}={threshold}")
    ratio = resource["width"] / resource["height"]
    pixels = resource["width"] * resource["height"]
    for name, value in (("aspect_ratio", ratio), ("total_pixels", pixels)):
        if limits.get(f"min_{name}") is not None and value < limits[f"min_{name}"]:
            raise ValueError(f"{label} violates configured min_{name}={limits[f'min_{name}']}")
        if limits.get(f"max_{name}") is not None and value > limits[f"max_{name}"]:
            raise ValueError(f"{label} violates configured max_{name}={limits[f'max_{name}']}")
    if resource["type"] == "video":
        seconds = resource["duration"] / 1000
        if seconds < limits.get("min_duration", 0) or seconds > limits.get("max_duration", math.inf):
            raise ValueError(f"{label}.duration is outside the configured reference-video range (milliseconds required)")
    formats = limits.get("support_formats")
    if formats and resource.get("format") and resource["format"] not in formats:
        raise ValueError(f"{label}.format must be one of {', '.join(formats)}")
    if formats and not resource.get("format"):
        warnings.append(f"{label}: format is absent; configured format constraints were not checked")
    if limits.get("max_file_size"):
        if "file_size_bytes" in resource:
            # The page expresses file sizes in MB; MiB is the conservative boundary.
            if resource["file_size_bytes"] > limits["max_file_size"] * 1024 * 1024:
                raise ValueError(f"{label} exceeds configured max_file_size={limits['max_file_size']} MB")
        else:
            warnings.append(f"{label}: byte size is absent; configured upload-size constraint was not checked")
    return warnings


def validate_request(spec: dict) -> dict:
    """Validate and normalize a request without reading credentials or making requests.

    References use uploaded resource dictionaries; video duration is milliseconds.
    Generated-video duration is an integer number of seconds. Unknown model/options
    raise ValueError instead of falling back to a different model.
    """
    if not isinstance(spec, dict):
        raise ValueError("Generation request must be an object")
    allowed = {"prompt", "model", "duration", "ratio", "resolution", "mode", "first_frame", "end_frame", "references", "seed", "warnings", "experimental", "verification_status"}
    unknown = set(spec) - allowed
    if unknown:
        raise ValueError("Unknown generation fields: " + ", ".join(sorted(map(str, unknown))))
    result = {k: deepcopy(v) for k, v in spec.items() if k not in {"warnings", "experimental", "verification_status"}}
    model = model_config(result.get("model", DEFAULT_MODEL))
    result["model"] = model["id"]
    prompt = result.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("prompt must be a non-empty string")
    result['prompt'] = prompt.strip()
    if len(prompt.encode("utf-16-le", errors="surrogatepass")) // 2 > model["max_prompt_length"]:
        raise ValueError(f"prompt exceeds {model['max_prompt_length']} UTF-16 characters for {model['id']}")
    duration = result.setdefault("duration", model["duration"]["default"])
    if isinstance(duration, bool) or not isinstance(duration, int) or not model["duration"]["min"] <= duration <= model["duration"]["max"]:
        raise ValueError(f"duration must be an integer from {model['duration']['min']} to {model['duration']['max']} seconds")
    ratio = result.setdefault("ratio", "16:9")
    if ratio not in model["ratios"]:
        raise ValueError("ratio must be one of " + ", ".join(model["ratios"]))
    resolution = result.setdefault("resolution", "720p")
    matches = [r for r in model["resolutions"] if isinstance(resolution, str) and r.lower() == resolution.lower()]
    if not matches:
        raise ValueError(f"resolution for {model['id']} must be one of " + ", ".join(model["resolutions"]))
    result["resolution"] = matches[0]
    mode = result.setdefault("mode", "omni")
    if not isinstance(mode, str) or mode not in {"omni", "frames"}:
        raise ValueError("mode must be omni or frames; long-video/edit are separate unimplemented workflows")
    references = result.setdefault("references", [])
    if not isinstance(references, list):
        raise ValueError("references must be a list of uploaded resource objects")
    if mode == "frames" and references:
        raise ValueError("frames mode uses first_frame/end_frame; references are for omni mode")
    if mode == "omni" and (result.get("first_frame") is not None or result.get("end_frame") is not None):
        raise ValueError("first_frame/end_frame require mode=frames")
    if mode == "frames" and result.get("first_frame") is None:
        raise ValueError("frames mode requires first_frame; end_frame is optional")
    warnings = []
    if mode == "frames":
        for key in ("first_frame", "end_frame"):
            if result.get(key) is not None:
                result[key] = _resource(result[key], key, "image")
        warnings.append("Frame-mode resource constraints and submission were not captured in the baseline HAR")
    else:
        result["references"] = [_resource(resource, f"references[{i}]") for i, resource in enumerate(references)]
        material_config = model["materials"]
        if len(references) > material_config["max_total_count"]:
            raise ValueError(f"references exceeds max_total_count={material_config['max_total_count']}")
        type_numbers = {"image": 1, "video": 2}
        by_type = {item["material_type"]: item["limit"] for item in material_config["supported_materials"]}
        for kind, number in type_numbers.items():
            resources = [r for r in result["references"] if r["type"] == kind]
            limits = by_type.get(number, {})
            if len(resources) > limits.get("max_count", 0):
                raise ValueError(f"Too many {kind} references; configured maximum is {limits.get('max_count', 0)}")
        for i, resource in enumerate(result["references"]):
            warnings.extend(_check_material(resource, by_type[type_numbers[resource["type"]]], f"references[{i}]"))
        video_ms = sum(r["duration"] for r in result["references"] if r["type"] == "video")
        if video_ms > material_config["max_total_video_duration"] * 1000:
            raise ValueError(f"Total reference-video duration exceeds {material_config['max_total_video_duration']} seconds")
        if references:
            warnings.append("Omni material/meta structures are derived from frontend code; this combination has not been verified by live generation")
        if video_ms:
            warnings.append("Input-video billing amounts are reconstructed, not verified against a captured video-reference submission")
    if "seed" in result:
        seed = result["seed"]
        if isinstance(seed, bool) or not isinstance(seed, int) or not 0 <= seed <= 2**53 - 1:
            raise ValueError("seed must be a non-negative JavaScript-safe integer (0..2^53-1)")
    baseline = model["id"] == DEFAULT_MODEL and duration == 4 and ratio == "16:9" and result["resolution"] == "720p" and mode == "omni" and not references
    if not baseline:
        warnings.append("Model/options are advertised in the captured configuration but are not this HAR's observed request combination")
    if result["resolution"].lower() in {"2k", "4k"}:
        warnings.append("EXPERIMENTAL: 2K/4K transport may require an additional super-resolution workflow; direct resolution submission is unverified")
    result["experimental"] = not baseline
    result["verification_status"] = "request-observed" if baseline else "static-config-only"
    result["warnings"] = warnings
    return result
