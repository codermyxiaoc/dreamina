"""Offline protocol/constraint tests. Nothing here submits a generation request."""

from copy import deepcopy
import json
from pathlib import Path
import unittest
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch

from dreamina_api.models import get_models, validate_request
from dreamina_api.payloads import build_generation


ROOT = Path(__file__).resolve().parents[1]


def image(**extra):
    return {"type": "image", "image_uri": "test-imagex-bucket/test.png", "width": 1280, "height": 720, "format": "png", **extra}


def video(**extra):
    return {"type": "video", "vid": "test-uploaded-video", "width": 1280, "height": 720, "fps": 24, "duration": 3000, "format": "mp4", **extra}


def unpack(request):
    draft = json.loads(request["body"]["draft_content"])
    component = draft["component_list"][0]
    params = component["abilities"]["gen_video"]["text_to_video_params"]
    return draft, component, params, params["video_gen_inputs"][0]


class ModelValidationTests(unittest.TestCase):
    def test_four_model_ids_and_no_silent_fallback(self):
        expected = {
            "seedance-2.5": "dreamina_seedance_45_pro",
            "seedance-2.0-mini": "dreamina_seedance_40_mini",
            "seedance-2.0-fast": "dreamina_seedance_40",
            "seedance-2.0": "dreamina_seedance_40_pro",
        }
        self.assertEqual({m["id"]: m["model_req_key"] for m in get_models()}, expected)
        for alias, upstream in expected.items():
            self.assertEqual(validate_request({"prompt": "hello", "model": upstream})["model"], alias)
            self.assertEqual(build_generation({"prompt": "hello", "model": alias})["body"]["extend"]["root_model"], upstream)
        with self.assertRaisesRegex(ValueError, "Unknown model"):
            validate_request({"prompt": "hello", "model": "seedance-next"})

    def test_model_metadata_does_not_mutate_the_snapshot(self):
        models = get_models()
        models[0]["resolutions"].clear()
        self.assertEqual(get_models()[0]["resolutions"], ["480p", "720p", "1080p"])

    def test_duration_and_resolution_are_model_specific(self):
        validate_request({"prompt": "test", "model": "seedance-2.5", "duration": 30, "resolution": "480p"})
        for invalid in (
            {"duration": 3}, {"duration": 16}, {"duration": True}, {"duration": 4.5},
            {"model": "seedance-2.5", "duration": 31},
            {"model": "seedance-2.5", "resolution": "4k"},
            {"model": "seedance-2.0", "resolution": "2k"},
            {"model": "seedance-2.0-fast", "resolution": "480p"},
            {"ratio": "2:1"}, {"mode": "long_video"}, {"mode": []},
        ):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                validate_request({"prompt": "test", **invalid})
        normalized = validate_request({"prompt": "test", "resolution": "2k"})
        self.assertEqual(normalized["resolution"], "2K")
        self.assertTrue(normalized["experimental"])
        self.assertTrue(any("EXPERIMENTAL" in warning for warning in normalized["warnings"]))

    def test_unicode_prompt_length_uses_frontend_utf16_units(self):
        validate_request({"prompt": "海" * 4000})
        with self.assertRaises(ValueError):
            validate_request({"prompt": "😀" * 2001})
        validate_request({"prompt": "海" * 15000, "model": "seedance-2.5"})
        for prompt in (" ", "", None, 42):
            with self.subTest(prompt=prompt), self.assertRaises(ValueError):
                validate_request({"prompt": prompt})

    def test_reference_counts_and_duration_constraints(self):
        validate_request({"prompt": "test", "references": [image()] * 9})
        for refs in ([image()] * 10, [video()] * 4, [video(duration=8000), video(duration=8000)], [video(duration=1999)]):
            with self.subTest(count=len(refs)), self.assertRaises(ValueError):
                validate_request({"prompt": "test", "references": refs})
        validate_request({"prompt": "test", "model": "seedance-2.5", "references": [video(duration=30200)]})
        with self.assertRaises(ValueError):
            validate_request({"prompt": "test", "model": "seedance-2.5", "references": [video(duration=30201)]})

    def test_seedance25_material_limits_are_enforced(self):
        for resource in (
            image(width=299), image(width=6001), image(width=300, height=1200),
            image(format="svg"), image(file_size_bytes=31 * 1024 * 1024),
            video(fps=23), video(fps=61), video(width=600, height=600),
            video(width=4000, height=3000), video(format="avi"),
            video(file_size_bytes=201 * 1024 * 1024),
        ):
            with self.subTest(resource=resource), self.assertRaises(ValueError):
                validate_request({"prompt": "test", "model": "seedance-2.5", "references": [resource]})

    def test_modes_cannot_silently_drop_media(self):
        for invalid in (
            {"mode": "frames"}, {"mode": "frames", "end_frame": image()},
            {"mode": "omni", "first_frame": image()},
            {"mode": "frames", "first_frame": image(), "references": [image()]},
            {"mode": "frames", "first_frame": video()},
        ):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                validate_request({"prompt": "test", **invalid})

    def test_incomplete_resource_or_url_is_rejected(self):
        for resource in ({"url": "https://example.org/image.png"}, image(image_uri="https://example.org/image.png"), video(duration=float("nan")), video(vid="")):
            with self.subTest(resource=resource), self.assertRaises(ValueError):
                validate_request({"prompt": "test", "references": [resource]})


class PayloadTests(unittest.TestCase):
    def test_baseline_body_and_query_have_no_captured_credentials(self):
        request = build_generation({"prompt": "A ceramic robot on a beach"}, workspace_id="12345", submit_id="my-task")
        draft, component, params, video_input = unpack(request)
        self.assertEqual(request["submit_id"], "my-task")
        self.assertEqual(request["query"]["generate_id"], "my-task")
        self.assertEqual(request["body"]["extend"]["workspace_id"], 12345)
        self.assertEqual(request["body"]["extend"]["m_video_commerce_info"]["amount"], 4)
        self.assertEqual(draft["main_component_id"], component["id"])
        self.assertEqual(video_input["duration_ms"], 4000)
        self.assertNotIn("unified_edit_input", video_input)
        self.assertIsInstance(params["seed"], int)
        self.assertEqual(request["verification_status"], "request-observed")
        for forbidden in ("msToken", "X-Bogus", "X-Gnarly", "sessionid", "user_id", "device_id"):
            self.assertNotIn(forbidden, request["query"])
            self.assertNotIn(forbidden, request["body"])
        from urllib.parse import unquote
        business = json.loads(unquote(request["query"]["babi_param"]))
        self.assertEqual(business["extra_param"], {"model_id": "dreamina_seedance_40_mini", "generate_type": "t2v"})
        self.assertEqual(business["feature_key"], "text_to_video")
        self.assertEqual(json.loads(request["body"]["metrics_extra"])["originSubmitId"], "my-task")
        self.assertEqual(component["metadata"]["created_did"], "")

    def test_fresh_ids_seeds_and_input_immutability(self):
        spec = {"prompt": "hello", "references": [image()]}
        original = deepcopy(spec)
        first = build_generation(spec)
        second = build_generation(spec)
        self.assertEqual(spec, original)
        self.assertNotEqual(first["submit_id"], second["submit_id"])
        self.assertNotEqual(unpack(first)[0]["id"], unpack(second)[0]["id"])
        explicit = build_generation({"prompt": "hello", "seed": 0})
        self.assertEqual(unpack(explicit)[2]["seed"], 0)
        self.assertEqual(json.loads(explicit["body"]["metrics_extra"])["isDefaultSeed"], 0)

    def test_omni_resources_and_metadata_index_mapping(self):
        wrapped = {"material_type": "image", "image_info": image()}
        request = build_generation({"prompt": "Move the object", "references": [wrapped, video()]})
        draft, component, params, video_input = unpack(request)
        unified = video_input["unified_edit_input"]
        self.assertEqual(draft["min_version"], "3.3.9")
        self.assertIn("AIGC_Video_UnifiedEdit", draft["min_features"])
        self.assertEqual([x["material_type"] for x in unified["material_list"]], ["image", "video"])
        self.assertEqual([x["material_ref"]["material_idx"] for x in unified["meta_list"][:2]], [0, 1])
        self.assertEqual(unified["meta_list"][-1]["text"], "Move the object")
        self.assertEqual(unified["material_list"][1]["video_info"]["duration"], 3000)
        self.assertNotIn("file_size_bytes", unified["material_list"][0]["image_info"])
        self.assertTrue(request["experimental"])
        self.assertTrue(any("billing" in warning for warning in request["warnings"]))
        metrics = json.loads(component["abilities"]["gen_video"]["video_task_extra"])
        scene = json.loads(metrics["sceneOptions"])[0]
        self.assertTrue(scene["hasInputVideo"])
        self.assertEqual(scene["inputVideoDuration"], 3)

    def test_frame_resources_and_version_gates(self):
        request = build_generation({"prompt": "transition", "mode": "frames", "first_frame": image(), "end_frame": image(image_uri="test-bucket/end.png"), "resolution": "1080p"})
        draft, _, _, video_input = unpack(request)
        self.assertIn("first_frame_image", video_input)
        self.assertIn("end_frame_image", video_input)
        self.assertNotIn("unified_edit_input", video_input)
        self.assertEqual(draft["min_version"], "3.2.4")
        seedance25 = build_generation({"prompt": "test", "model": "seedance-2.5"})
        self.assertIn("AIGC_Video_Seedance25ResultAction", unpack(seedance25)[0]["min_features"])
        self.assertEqual(seedance25["body"]["extend"]["m_video_commerce_info"]["benefit_type"], "seedance_25_720p_no_input_video_output")

    def test_optional_workspace_is_not_replaced_by_captured_id(self):
        result = build_generation({"prompt": "hello"})
        self.assertNotIn("workspace_id", result["body"]["extend"])
        self.assertTrue(any("workspace_id" in warning for warning in result["warnings"]))
        for value in ("bad", "0", "-1", True):
            with self.subTest(value=value), self.assertRaises(ValueError):
                build_generation({"prompt": "hello"}, workspace_id=value)

    def test_payload_generation_needs_neither_har_nor_browser(self):
        # Config was loaded from packaged data on import; construction never opens files.
        with patch("builtins.open", side_effect=AssertionError("Unexpected file read")):
            result = build_generation({"prompt": "offline protocol construction"})
        self.assertEqual(result["body"]["http_common_info"], {"aid": 513641})

    @unittest.skipUnless((ROOT / "evidence" / "dreamina.capcut.com.har").exists(), "Private local HAR is not distributed with the package")
    def test_protocol_shape_matches_observed_har_entry3(self):
        # Read only this request, and never print or preserve its identifiers/tokens.
        entries = json.loads((ROOT / "evidence" / "dreamina.capcut.com.har").read_text(encoding="utf-8-sig"))["log"]["entries"]
        observed_request = entries[3]["request"]
        observed = json.loads(observed_request["postData"]["text"])
        actual = build_generation({"prompt": "test-only prompt", "seed": 123}, workspace_id="12345")
        self.assertEqual(set(actual["body"]), set(observed))
        self.assertEqual(set(actual["body"]["extend"]), set(observed["extend"]))
        self.assertEqual(actual["body"]["http_common_info"], observed["http_common_info"])
        self.assertEqual(actual["body"]["extend"]["m_video_commerce_info"], observed["extend"]["m_video_commerce_info"])
        observed_draft = json.loads(observed["draft_content"])
        draft, component, params, video_input = unpack(actual)
        observed_component = observed_draft["component_list"][0]
        observed_params = observed_component["abilities"]["gen_video"]["text_to_video_params"]
        observed_input = observed_params["video_gen_inputs"][0]
        self.assertEqual(set(draft), set(observed_draft))
        self.assertEqual(set(component), set(observed_component))
        self.assertEqual(set(params), set(observed_params))
        self.assertEqual(set(video_input), set(observed_input))
        for key in ("version", "type", "min_version", "min_features", "is_from_tsn"):
            self.assertEqual(draft[key], observed_draft[key])
        for key in ("fps", "video_mode", "duration_ms", "resolution", "idip_meta_list", "min_version"):
            self.assertEqual(video_input[key], observed_input[key])
        observed_query = parse_qs(urlsplit(observed_request["url"]).query)
        for key, value in actual["query"].items():
            if key != "generate_id":
                self.assertEqual(value, observed_query[key][0])
        self.assertNotEqual(actual["body"]["submit_id"], observed["submit_id"])
        self.assertNotEqual(component["metadata"]["created_time_in_ms"], observed_component["metadata"]["created_time_in_ms"])


if __name__ == "__main__":
    unittest.main()
