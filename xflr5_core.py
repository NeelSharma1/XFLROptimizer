"""Shared non-GUI XFLR5 helpers.

The GUI adapters import this module so that the legacy adapter can be moved
out of the main optimization module without creating a circular import.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from math import isfinite
from pathlib import Path


class XFLRGuiError(RuntimeError):
    """Raised when a GUI adapter cannot find the expected XFLR5 state."""


class ConstraintError(ValueError):
    """Raised when a candidate violates the configured design restrictions."""


class LengthUnitError(ValueError):
    """Raised when a user-entered length cannot be converted."""


_LENGTH_UNITS_TO_METERS = {
    "": None,
    "native": None,
    "xflr": None,
    "m": 1.0,
    "meter": 1.0,
    "meters": 1.0,
    "metre": 1.0,
    "metres": 1.0,
    "cm": 0.01,
    "centimeter": 0.01,
    "centimeters": 0.01,
    "centimetre": 0.01,
    "centimetres": 0.01,
    "mm": 0.001,
    "millimeter": 0.001,
    "millimeters": 0.001,
    "millimetre": 0.001,
    "millimetres": 0.001,
    "in": 0.0254,
    "inch": 0.0254,
    "inches": 0.0254,
    "ft": 0.3048,
    "foot": 0.3048,
    "feet": 0.3048,
    "yd": 0.9144,
    "yard": 0.9144,
    "yards": 0.9144,
}
_LENGTH_EXPRESSION = re.compile(
    r"^\s*([+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)\s*([A-Za-z]*)\s*$"
)


def convert_length(value: str | float | int, native_length_unit_to_meter: float) -> float:
    """Convert a user length expression to the native XFLR XML unit.

    Unitless values are already native XFLR values. For example, with an XML
    scale of ``0.0254`` metres per native unit, ``convert_length('5 feet',
    0.0254)`` returns ``60.0``.
    """

    if native_length_unit_to_meter <= 0 or not isfinite(native_length_unit_to_meter):
        raise LengthUnitError("The XML length-unit scale must be a finite positive number.")
    if isinstance(value, (int, float)):
        magnitude = float(value)
        unit = ""
    else:
        match = _LENGTH_EXPRESSION.fullmatch(value)
        if match is None:
            raise LengthUnitError(f"Invalid length {value!r}; use forms such as '5 feet', '1.2 m', or '60'.")
        magnitude = float(match.group(1))
        unit = match.group(2).lower()
    if not isfinite(magnitude):
        raise LengthUnitError(f"Length must be finite: {value!r}.")
    if unit not in _LENGTH_UNITS_TO_METERS:
        supported = ", ".join(("m", "cm", "mm", "in", "ft", "yd", "native"))
        raise LengthUnitError(f"Unsupported length unit {unit!r}; use {supported}.")
    meters_per_unit = _LENGTH_UNITS_TO_METERS[unit]
    return magnitude if meters_per_unit is None else magnitude * meters_per_unit / native_length_unit_to_meter


class PlaneXmlEditor:
    """Edit XFLR-exported plane or component XML without relying on line order."""

    @staticmethod
    def length_unit_to_meter(xml_path: str | Path) -> float:
        """Read XFLR's native length scale from an exported plane/component XML."""

        tree = ET.parse(xml_path)
        value = tree.findtext("./Units/length_unit_to_meter")
        if value is None:
            value = tree.findtext(".//Units/length_unit_to_meter")
        if value is None:
            raise LengthUnitError(f"No Units/length_unit_to_meter field was found in {xml_path}.")
        try:
            scale = float(value)
        except ValueError as error:
            raise LengthUnitError(f"Invalid XML length-unit scale {value!r} in {xml_path}.") from error
        if scale <= 0 or not isfinite(scale):
            raise LengthUnitError(f"XML length-unit scale must be finite and positive in {xml_path}.")
        return scale

    @staticmethod
    def _wings(root: ET.Element, component_name: str | None = None) -> list[ET.Element]:
        wings = list(root.findall(".//wing"))
        if component_name is None:
            return wings
        return [wing for wing in wings if (wing.findtext("Name") or "").strip().lower() == component_name.lower()]

    @classmethod
    def _target_wing(cls, root: ET.Element, component_name: str | None = None) -> ET.Element:
        wings = cls._wings(root, component_name)
        if len(wings) != 1:
            description = component_name or "component"
            raise ConstraintError(f"Expected exactly one wing for {description!r}, found {len(wings)}.")
        return wings[0]

    @staticmethod
    def _selected_sections(wing: ET.Element, sections: tuple[int, int | None]) -> list[ET.Element]:
        start, end = sections
        if start < 1 or (end is not None and end < start):
            raise ConstraintError("sections must use one-indexed inclusive bounds, e.g. (1, None).")
        selected = list(wing.findall("./Sections/Section"))[start - 1 : end]
        if not selected:
            raise ConstraintError("The requested section range does not select any XML sections.")
        return selected

    @classmethod
    def replace_airfoils(
        cls,
        xml_path: str | Path,
        left_airfoil: str,
        right_airfoil: str | None = None,
        sections: tuple[int, int | None] = (1, None),
        component_name: str | None = None,
    ) -> None:
        tree = ET.parse(xml_path)
        wing = cls._target_wing(tree.getroot(), component_name)
        for section in cls._selected_sections(wing, sections):
            left = section.find("Left_Side_FoilName")
            right = section.find("Right_Side_FoilName")
            if left is None or right is None:
                raise ConstraintError("The selected XFLR section has no left/right airfoil fields.")
            left.text = left_airfoil
            right.text = right_airfoil or left_airfoil
        ET.indent(tree, space="    ")
        tree.write(xml_path, encoding="utf-8", xml_declaration=True)

    @classmethod
    def span(cls, xml_path: str | Path, component_name: str | None = None) -> float:
        tree = ET.parse(xml_path)
        wing = cls._target_wing(tree.getroot(), component_name)
        y_positions = [float(section.findtext("y_position")) for section in wing.findall("./Sections/Section")]
        if not y_positions:
            raise ConstraintError("The wing has no sections.")
        symmetric = (wing.findtext("Symetric") or "false").strip().lower() == "true"
        return 2 * max(abs(value) for value in y_positions) if symmetric else max(y_positions) - min(y_positions)

    @classmethod
    def set_span(cls, xml_path: str | Path, target_span: float, component_name: str | None = None) -> None:
        if target_span <= 0:
            raise ConstraintError("span must be positive.")
        current_span = cls.span(xml_path, component_name)
        if current_span <= 0:
            raise ConstraintError("Cannot scale a wing whose current span is zero.")
        tree = ET.parse(xml_path)
        wing = cls._target_wing(tree.getroot(), component_name)
        scale = target_span / current_span
        for section in wing.findall("./Sections/Section"):
            y_position = section.find("y_position")
            if y_position is None or y_position.text is None:
                raise ConstraintError("The selected XFLR section has no y_position.")
            y_position.text = f"{float(y_position.text) * scale:.6f}"
        ET.indent(tree, space="    ")
        tree.write(xml_path, encoding="utf-8", xml_declaration=True)
