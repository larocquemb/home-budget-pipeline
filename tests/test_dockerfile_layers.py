from pathlib import Path


def test_ocr_base_uses_x86_64_v2_compatible_ubi9():
    dockerfile = Path("Dockerfile.ocr-base").read_text(encoding="utf-8")

    assert "ubi9/python-312-minimal:9.6" in dockerfile
    assert "ubi10/" not in dockerfile


def test_runtime_dependencies_are_cached_before_application_source():
    dockerfile = Path("Dockerfile").read_text(encoding="utf-8")

    metadata_copy = dockerfile.index("COPY pyproject.toml README.md ./")
    dependency_install = dockerfile.index("'.[db,paddle]'")
    source_copy = dockerfile.index("COPY src ./src")
    project_install = dockerfile.index("--no-deps --no-build-isolation --force-reinstall")

    assert metadata_copy < dependency_install < source_copy < project_install


def test_project_layer_does_not_resolve_dependencies_again():
    dockerfile = Path("Dockerfile").read_text(encoding="utf-8")
    project_layer = dockerfile[dockerfile.index("COPY src ./src") :]

    assert "--no-deps" in project_layer


def test_paddle_native_runtime_is_installed_before_model_preload():
    dockerfile = Path("Dockerfile").read_text(encoding="utf-8")

    native_libraries = dockerfile.index("microdnf install -y libgomp mesa-libGL")
    paddle_preload = dockerfile.index("from paddleocr import PaddleOCR")

    assert native_libraries < paddle_preload
