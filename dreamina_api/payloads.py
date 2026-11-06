"""Construct unsigned Dreamina video requests from observed protocol structures.

No browser, network, token, user ID, or captured one-time signature is used here.
Callers supply credentials in their transport layer and uploaded media resources.
"""

from __future__ import annotations

from copy import deepcopy
import json
import secrets
import time
import uuid
from urllib.parse import quote

from .models import DRAFT_VERSION, model_config, validate_request


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _uuid() -> str:
    return str(uuid.uuid4())


def _node(kind: str = "", **values) -> dict:
    return {"type": kind, "id": _uuid(), **values}


def _media(resource: dict) -> dict:
    common = {"source_from", "name", "width", "height"}
    image_fields = {"image_uri", "uri", "platform_type", "format", "ever_photo", "aigc_image", "algo_extra", "title", "link", "is_animated"}
    video_fields = {"vid", "fps", "duration", "aigc_video", "cover_image_url", "source_start_ms", "source_duration_ms"}
    kind = resource["type"]
    values = {k: deepcopy(v) for k, v in resource.items() if k in common | (image_fields if kind == "image" else video_fields)}
    values.setdefault("source_from", "upload")
    values.setdefault("name", "")
    if kind == "image":
        values.setdefault("platform_type", 1)
        values.setdefault("format", "")
    else:
        values.setdefault("cover_image_url", "")
    return _node(kind, **values)


def _commerce(model: dict, spec: dict, input_video_seconds: float) -> dict:
    candidates = model["commerce"]["resolution_price_configs"]
    selected = next(p for p in candidates if p["resolution"].lower() == spec["resolution"].lower())
    # The 2.5 configuration differentiates no-video pricing. Other models use
    # the configured per-resolution base price; input-video charging needs a live
    # quote/verification and is explicitly identified as experimental to callers.
    key = "no_input_video_price" if not input_video_seconds and "no_input_video_price" in selected else "price"
    result = deepcopy(selected[key])
    result["amount"] = spec["duration"] + input_video_seconds
    if isinstance(result["amount"], float) and result["amount"].is_integer():
        result["amount"] = int(result["amount"])
    return result


def build_generation(spec: dict, *, workspace_id: str | None = None, submit_id: str | None = None) -> dict:
    """Return body/query/submit_id/model plus explicit verification metadata.

    The body can be POSTed to /mweb/v1/aigc_draft/generate after the transport
    applies session authentication and any required fresh signing. ``query``
    deliberately excludes msToken, X-Bogus and X-Gnarly. Business metadata in
    babi_param is reconstructed from the request, never copied from a capture.
    A numeric workspace ID is emitted as a JSON integer; absence is preserved.
    """
    spec = validate_request(spec)
    model = model_config(spec["model"])
    if submit_id is None:
        submit_id = _uuid()
    elif not isinstance(submit_id, str) or not submit_id.strip() or len(submit_id) > 128:
        raise ValueError("submit_id must be a non-empty string up to 128 characters")
    if workspace_id is not None:
        if isinstance(workspace_id, bool) or not isinstance(workspace_id, (str, int)) or not str(workspace_id).isdigit():
            raise ValueError("workspace_id must contain decimal digits")
        if int(workspace_id) <= 0:
            raise ValueError("workspace_id must be positive")
    resources = spec["references"]
    input_video_seconds = sum(r["duration"] for r in resources if r["type"] == "video") / 1000
    material_types = [1 if r["type"] == "image" else 2 for r in resources]
    if spec["mode"] == "frames":
        material_types = [1 for key in ("first_frame", "end_frame") if spec.get(key) is not None]
    scene = {
        "type": "video", "scene": "BasicVideoGenerateButton",
        "resolution": spec["resolution"], "modelReqKey": model["model_req_key"],
        "videoDuration": spec["duration"], "batchNumber": 1,
        "inputVideoDuration": input_video_seconds, "chargeInputVideoDuration": True,
        "isLongVideo": False, "hasInputVideo": input_video_seconds > 0,
        "useSeedanceFast5sFreeTrial": False,
        "reportParams": {
            "enterSource": "generate", "vipSource": "generate",
            "extraVipFunctionKey": f"{model['model_req_key']}-{spec['resolution']}",
            "useVipFunctionDetailsReporterHoc": True, "cashierBusinessExtra": {},
        },
        "materialTypes": material_types,
    }
    metrics = {
        "isDefaultSeed": int("seed" not in spec), "originSubmitId": submit_id,
        "isRegenerate": False, "enterFrom": "api", "position": "page_bottom_box",
        "promptType": "original_prompt",
        "functionMode": "omni_reference" if spec["mode"] == "omni" else "first_last_frames",
        "generatorFeature": "omniReference" if spec["mode"] == "omni" else "firstLastFrames",
        "sceneOptions": _json([scene]), "batchNumber": 1,
        "submitGroupId": _uuid(), "hasRejectedAudit": 0,
    }
    metrics_json = _json(metrics)
    video_input = _node(
        min_version="3.0.5", prompt=spec["prompt"], video_mode=2, fps=model["fps"],
        duration_ms=spec["duration"] * 1000, resolution=spec["resolution"], idip_meta_list=[],
    )
    min_features = []
    min_version = "3.0.5"
    if spec["resolution"] == "1080p":
        # Recorded static FeaturesMinVersion.VideoResolution1080P.
        min_version = "3.2.4"
        video_input["min_version"] = min_version
    if spec["mode"] == "frames":
        for key, target in (("first_frame", "first_frame_image"), ("end_frame", "end_frame_image")):
            if spec.get(key) is not None:
                video_input[target] = _media(spec[key])
    elif resources:
        material_list, meta_list = [], []
        for index, resource in enumerate(resources):
            kind = resource["type"]
            material_list.append(_node(material_type=kind, **{f"{kind}_info": _media(resource)}))
            meta_list.append(_node(meta_type=kind, text="", material_ref=_node(material_idx=index)))
        meta_list.append(_node(meta_type="text", text=spec["prompt"]))
        video_input["unified_edit_input"] = _node(material_list=material_list, meta_list=meta_list)
        min_version = video_input["min_version"] = "3.3.9"
        min_features.append("AIGC_Video_UnifiedEdit")
    if model["model_req_key"].startswith("dreamina_seedance_45"):
        min_features.append("AIGC_Video_Seedance25ResultAction")
    seed = spec.get("seed", secrets.randbelow(2**31))
    params = _node(video_gen_inputs=[video_input], video_aspect_ratio=spec["ratio"], seed=seed, model_req_key=model["model_req_key"], priority=0)
    component = _node(
        "video_base_component", min_version="1.0.0", aigc_mode="workbench",
        metadata=_node(created_platform=3, created_platform_version="", created_time_in_ms=str(time.time_ns() // 1_000_000), created_did=""),
        generate_type="gen_video",
        abilities=_node(gen_video=_node(text_to_video_params=params, video_task_extra=metrics_json)),
        process_type=1,
    )
    draft = _node(
        "draft", min_version=min_version, min_features=min_features,
        is_from_tsn=True, version=DRAFT_VERSION,
        main_component_id=component["id"], component_list=[component],
    )
    commerce = _commerce(model, spec, input_video_seconds)
    extend = {"root_model": model["model_req_key"], "m_video_commerce_info": commerce, "m_video_commerce_info_list": [deepcopy(commerce)]}
    warnings = list(spec["warnings"])
    if workspace_id is not None:
        extend["workspace_id"] = int(workspace_id)
    else:
        warnings.append("workspace_id was omitted; whether it is optional for this account has not been verified")
    body = {
        "extend": extend, "submit_id": submit_id, "metrics_extra": metrics_json,
        "draft_content": _json(draft), "http_common_info": {"aid": 513641},
    }
    query = {
        "aid": "513641", "device_platform": "web", "region": "US",
        "da_version": DRAFT_VERSION, "os": "windows", "web_component_open_flag": "1",
        "generate_id": submit_id, "commerce_with_input_video": "1",
        "web_version": "7.5.0", "aigc_features": "app_lip_sync",
    }
    feature = "text_to_video" if not resources and spec["mode"] == "omni" else "image_to_video"
    query["babi_param"] = quote(_json({
        "feature_entrance": "to-generate", "feature_entrance_detail": f"to-generate-{feature}",
        "feature_key": feature, "scenario": "image_video_generation", "edit_type": "tool",
        "tool_id": "tool_video", "sub_tool_id": "tool_video", "tab_name": "tool", "enter_from": "tool",
        "template_id": "", "scene_lv1": "tool", "scene_lv2": "tool_video",
        "extra_param": {"model_id": model["model_req_key"], "generate_type": "t2v" if feature == "text_to_video" else "i2v"},
    }), safe="")
    return {
        "body": body, "query": query, "submit_id": submit_id,
        "model": spec["model"], "model_req_key": model["model_req_key"],
        "seed": seed, "endpoint": "/mweb/v1/aigc_draft/generate",
        "experimental": spec["experimental"], "verification_status": spec["verification_status"],
        "warnings": warnings, "signing_status": "unsigned; captured one-time query signatures are deliberately excluded",
    }
