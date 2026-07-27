import unittest

from verify_local_docker_image import validate_local_image_inspect

_ID = f"sha256:{'1' * 64}"
_LAYER = f"sha256:{'2' * 64}"


def _image(**changes):
    image = {
        "Id": _ID,
        "Os": "linux",
        "Architecture": "amd64",
        "RootFS": {"Type": "layers", "Layers": [_LAYER]},
    }
    image.update(changes)
    return image


class LocalDockerImageVerificationTests(unittest.TestCase):
    def test_accepts_supported_single_manifest_descriptor(self) -> None:
        for media_type in (
            "application/vnd.oci.image.manifest.v1+json",
            "application/vnd.docker.distribution.manifest.v2+json",
        ):
            with self.subTest(media_type=media_type):
                result = validate_local_image_inspect(
                    [_image(Descriptor={"mediaType": media_type})],
                    reference="release-api:sha",
                )
                self.assertEqual(result["media_type"], media_type)

    def test_accepts_concrete_engine_image_when_descriptor_is_unavailable(self) -> None:
        result = validate_local_image_inspect(
            [_image()],
            reference="release-worker:sha",
        )
        self.assertEqual(result["media_type"], "docker-engine-single-platform")
        self.assertEqual(result["platform"], "linux/amd64")

    def test_rejects_manifest_index_descriptor(self) -> None:
        for media_type in (
            "application/vnd.oci.image.index.v1+json",
            "application/vnd.docker.distribution.manifest.list.v2+json",
        ):
            with self.subTest(media_type=media_type), self.assertRaisesRegex(
                ValueError,
                "single image manifest",
            ):
                validate_local_image_inspect(
                    [_image(Descriptor={"mediaType": media_type})],
                    reference="release-frontend:sha",
                )

    def test_rejects_descriptorless_record_without_concrete_rootfs(self) -> None:
        with self.assertRaisesRegex(ValueError, "concrete RootFS"):
            validate_local_image_inspect(
                [_image(RootFS={"Type": "layers", "Layers": []})],
                reference="release-api:sha",
            )

    def test_rejects_invalid_fallback_layer_digest(self) -> None:
        with self.assertRaisesRegex(ValueError, "concrete RootFS"):
            validate_local_image_inspect(
                [_image(RootFS={"Type": "layers", "Layers": ["not-a-digest"]})],
                reference="release-api:sha",
            )

    def test_rejects_invalid_image_identity_or_platform(self) -> None:
        for changes in (
            {"Id": "sha256:short"},
            {"Os": "windows"},
            {"Architecture": ""},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                validate_local_image_inspect(
                    [_image(**changes)],
                    reference="release-api:sha",
                )

    def test_rejects_multiple_inspect_records(self) -> None:
        with self.assertRaisesRegex(ValueError, "exactly one"):
            validate_local_image_inspect(
                [_image(), _image()],
                reference="release-api:sha",
            )


if __name__ == "__main__":
    unittest.main()
