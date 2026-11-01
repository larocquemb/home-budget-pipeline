"""One logical receipt namespace over configured scanned/electronic roots."""
import os
from pathlib import Path


def source_roots(root: Path):
    root = root.expanduser().resolve()
    roots = [('', root)]
    configured = os.getenv('RECEIPT_SOURCE_ROOT')
    electronic = os.getenv('ELECTRONIC_RECEIPT_SOURCE_ROOT')
    # A custom review/staging root must remain self-contained.
    if configured and electronic and root == Path(configured).expanduser().resolve():
        other = Path(electronic).expanduser().resolve()
        if other == root or other.is_relative_to(root) or root.is_relative_to(other):
            raise ValueError('Scanned and electronic receipt roots must be separate directories')
        roots.append(('electronic/', other))
    return roots


def reference_for(path: Path, root: Path) -> str:
    path = path.resolve()
    for prefix, physical_root in source_roots(root):
        if path.is_relative_to(physical_root):
            relative = path.relative_to(physical_root).as_posix()
            if not prefix and len(source_roots(root)) > 1 and relative.startswith('electronic/'):
                raise ValueError('electronic/ is reserved for the configured electronic source')
            return prefix + relative
    raise ValueError('Receipt source resolves outside the configured roots')


def resolve_reference(reference: str, root: Path) -> Path:
    roots = source_roots(root)
    prefix, physical_root = roots[1] if len(roots) > 1 and reference.startswith('electronic/') else roots[0]
    path = (physical_root / reference.removeprefix(prefix)).resolve()
    if not path.is_relative_to(physical_root):
        raise ValueError('source_reference resolves outside the receipt root')
    return path


def discover_sources(root: Path):
    from .ingest import discover_scans
    sources = []
    for _, physical_root in source_roots(root):
        if not physical_root.is_dir():
            raise ValueError(f'Receipt source directory does not exist: {physical_root}')
        for path in discover_scans(physical_root):
            reference_for(path, root)  # Reject symlinks escaping either root.
            sources.append(path)
    return sources
