"""Shared matplotlib style: reference categorical palette (fixed order), recessive axes, thin marks."""
from __future__ import annotations

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

SERIES = ["#2a78d6", "#eb6834", "#1baf7a"]   # slots 1-3: blue, orange, aqua (validated all-pairs)
NEUTRAL = "#b4b2ab"                           # "not on CPU / waiting": absence, not an identity
SURFACE = "#fcfcfb"
INK = "#0b0b0b"
INK_2 = "#52514e"
GRID = "#e4e3df"


def apply() -> None:
    plt.rcParams.update({
        "figure.facecolor": SURFACE, "axes.facecolor": SURFACE, "savefig.facecolor": SURFACE,
        "font.size": 10, "axes.titlesize": 11, "axes.titleweight": "bold", "axes.titlelocation": "left",
        "axes.edgecolor": GRID, "axes.labelcolor": INK_2, "text.color": INK,
        "xtick.color": INK_2, "ytick.color": INK_2, "xtick.labelsize": 9, "ytick.labelsize": 9,
        "axes.spines.top": False, "axes.spines.right": False,
        "axes.grid": True, "grid.color": GRID, "grid.linewidth": 0.6, "axes.axisbelow": True,
        "lines.linewidth": 2, "lines.markersize": 6,
        "legend.frameon": False, "legend.fontsize": 9,
        "savefig.dpi": 200, "savefig.bbox": "tight",
    })


def save(fig, path) -> None:
    fig.savefig(path)
    plt.close(fig)
