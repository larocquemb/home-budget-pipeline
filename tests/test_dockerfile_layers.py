from pathlib import Path


def test_runtime_dependencies_are_cached_before_application_source():
    dockerfile = Path("Dockerfile").read_text(encoding="utf-8")

    metadata_copy = dockerfile.index("COPY pyproject.toml README.md ./")
    dependency_install = dockerfile.index("'.[db]'")
    source_copy = dockerfile.index("COPY src ./src")
    project_install = dockerfile.index("--no-deps --no-build-isolation --force-reinstall")

    assert metadata_copy < dependency_install < source_copy < project_install


def test_project_layer_does_not_resolve_dependencies_again():
    dockerfile = Path("Dockerfile").read_text(encoding="utf-8")
    project_layer = dockerfile[dockerfile.index("COPY src ./src") :]

    assert "--no-deps" in project_layer
