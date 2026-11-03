"""Retain PaddleOCR recognition polygons without inventing absent geometry."""
from math import isfinite


def bounds(polygons,index):
    try:
        points=polygons[index]
        xs=[float(p[0]) for p in points]
        ys=[float(p[1]) for p in points]
        if len(xs)<4 or not all(isfinite(x) for x in xs+ys):
            return (None,)*4
        width,height=max(xs)-min(xs),max(ys)-min(ys)
        return (min(xs),min(ys),width,height) if width>0 and height>0 else (None,)*4
    except (IndexError,KeyError,TypeError,ValueError):
        return (None,)*4
