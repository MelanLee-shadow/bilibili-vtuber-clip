"""Keep a repaired cover on the route it was originally produced under.

治 C7（截图优先）：通用 cover-only 修复是**重绘独占**的
（`cover_repair.py` 三处硬编码 `selected_treatment="cpa_redraw"`，
`scripts/regenerate_channel_cover.py` 自述 "real CPA image edit only"），于是任何
一次返修都把已经选定的截图路线单向换成 AI 重绘——「一旦降级或返修，没有任何路径
能走回截图」。

两条出路按证据分流：

* 那份 hash-bound 截图像素**还在盘上** → 返修的正确工具是
  ``scripts/repair_screenshot_cover.py``（同一份已证据化的 PNG 重排海报卡、重渲
  标题、重打整脸门，且不再打生图），通用重绘必须在**付费请求之前**让路；
* 像素已经不在了 → 没有可继承的截图，重绘是唯一出路，不阻断交付；但被顶替的
  路线必须 typed 披露，不能静默消失。`actual_treatment` 永远如实记
  ``cpa_redraw``——这批字节确实是重绘，把它记成截图就是伪造。
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path


SCREENSHOT_ROUTE_TREATMENTS = ("screenshot_direct", "screenshot_polish")
SCREENSHOT_REPAIR_TOOL = "scripts/repair_screenshot_cover.py"
DISPLACEMENT_SCHEMA = "cover-repair-route-displacement.v1"
SCREENSHOT_REPAIR_REQUIRED = "COVER_SCREENSHOT_ROUTE_REPAIR_REQUIRED"


def prior_cover_route_treatment(generation: object) -> str:
    """Read the route a cover's pixels were **actually produced under**.

    键在 ``actual_treatment`` 而不是 ``selected_treatment``：一条被降级过的封面
    选的是截图、产出的是重绘，那份字节里没有可保留的截图。``actual_treatment``
    缺席（初始 v1 证据、或执行态还是 PENDING/BLOCKED）时退回 selected，只有两者
    都指向截图路线才算数。
    """

    if not isinstance(generation, Mapping):
        return ""
    route = generation.get("route_decision")
    if not isinstance(route, Mapping):
        return ""
    actual = str(route.get("actual_treatment") or "")
    if actual:
        # polish 被拒后降级成 direct 的成品仍然是**真截图**，必须算数；
        # 只有 actual 本身是 cpa_redraw 才说明那份字节里没有截图可保留。
        return actual
    selected = str(route.get("selected_treatment") or "")
    return selected if route.get("execution_status") in (None, "READY") else ""


def recoverable_screenshot_pixels(generation: Mapping[str, object]) -> Path | None:
    """The on-disk screenshot artifact a repair could recompose from.

    ``ai_background`` 是截图路线唯一被落进 generation 的路径（海报底板；
    `cover_polish_gate` 的 polish 回执只记 status，不记 polished 路径）。海报的
    同目录兄弟 ``*.screenshot-polished.png`` / ``*.screenshot-base.png`` 才是
    `scripts/repair_screenshot_cover.py --polished` 真正要吃的输入，所以优先报它们，
    都不在时退回海报——三者任一存在都证明这条截图的像素链还在盘上。
    """

    poster = Path(str(generation.get("ai_background") or ""))
    if not str(generation.get("ai_background") or ""):
        return None
    stem = poster.name.split(".")[0]
    for name in (
        f"{stem}.screenshot-polished.png",
        f"{stem}.screenshot-base.png",
    ):
        sibling = poster.with_name(name)
        if sibling.is_file():
            return sibling
    return poster if poster.is_file() else None


def recoverable_screenshot_route(rec: object) -> tuple[str, Path] | None:
    """Return (prior treatment, recomposable pixels) for a screenshot cover.

    权威是**活动记录自己的** ``cover_generation``（初始生产与历次返修都写在这里，
    见 `cover_repair._initial_cover_proof_valid` 读的同一处）。盘上的
    ``<cover>.cover_generation.json`` 只有 `scripts/regenerate_channel_cover.py`
    会写，是重绘 lane 的产物——拿它当权威，初次返修一条截图封面时它根本不存在，
    这道门就永远不会触发。
    """

    if not isinstance(rec, Mapping):
        return None
    generation = rec.get("cover_generation")
    treatment = prior_cover_route_treatment(generation)
    if treatment not in SCREENSHOT_ROUTE_TREATMENTS:
        return None
    assert isinstance(generation, Mapping)
    pixels = recoverable_screenshot_pixels(generation)
    return None if pixels is None else (treatment, pixels)


def refuse_recoverable_screenshot_route(rec: object) -> None:
    """Fail closed, cost-free, before a generic redraw displaces a screenshot."""

    recoverable = recoverable_screenshot_route(rec)
    if recoverable is None:
        return
    raise ValueError(
        f"{SCREENSHOT_REPAIR_REQUIRED}: the displaced route is "
        f"{recoverable[0]} and its hash-bound pixels are still at "
        f"{recoverable[1]}; screenshot repairs go through "
        f"{SCREENSHOT_REPAIR_TOOL}. A generic CPA redraw must not silently "
        "convert an already-selected screenshot cover into a redraw."
    )


def record_screenshot_route_displacement(
    enriched: dict, prior_generation: object
) -> str:
    """Disclose a displaced screenshot route; return the rationale suffix.

    ``prior_generation`` 必须是**返修之前**那份 cover generation（活动记录里的
    ``rec["cover_generation"]``）。传新生成的 manifest 没有意义：它是重绘 lane 刚
    写出来的，`route_decision` 永远是 cpa_redraw，披露永远不会触发。
    """

    prior_treatment = prior_cover_route_treatment(prior_generation)
    if prior_treatment not in SCREENSHOT_ROUTE_TREATMENTS:
        return ""
    enriched["cover_repair_route_displacement"] = {
        "schema_version": DISPLACEMENT_SCHEMA,
        "prior_selected_treatment": prior_treatment,
        "replaced_with": "cpa_redraw",
        "reason_code": "SCREENSHOT_SOURCE_PIXELS_UNAVAILABLE",
        "screenshot_repair_tool": SCREENSHOT_REPAIR_TOOL,
    }
    return (
        f"; displaced route {prior_treatment} could not be preserved because "
        "its screenshot pixels are no longer on disk"
    )
