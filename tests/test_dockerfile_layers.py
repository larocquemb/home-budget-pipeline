from pathlib import Path


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


def test_paddle_uses_headless_opencv_before_model_preload():
    dockerfile = Path("Dockerfile").read_text(encoding="utf-8")

    uninstall_gui_opencv = dockerfile.index("pip uninstall -y opencv-contrib-python")
    install_headless_opencv = dockerfile.index("opencv-contrib-python-headless==4.10.0.84")
    paddle_preload = dockerfile.index("from paddleocr import PaddleOCR")

    assert uninstall_gui_opencv < install_headless_opencv < paddle_preload


def test_paddle_native_runtime_is_installed_before_model_preload():
    dockerfile = Path("Dockerfile").read_text(encoding="utf-8")

    libgomp_install = dockerfile.index("microdnf install -y libgomp")
    paddle_preload = dockerfile.index("from paddleocr import PaddleOCR")

    assert libgomp_install < paddle_preload
