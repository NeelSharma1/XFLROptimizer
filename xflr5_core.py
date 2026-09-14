"""Shared non-GUI XFLR5 helpers.

The GUI adapters import this module so that the legacy adapter can be moved
out of the main optimization module without creating a circular import.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from math import isfinite, radians, tan
from pathlib import Path
from typing import Mapping


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

    SUPPORTED_GEOMETRY_PARAMETERS = frozenset(
        {
            "span",
            "chord_scale",
            "root_chord",
            "tip_chord",
            "taper_ratio",
            "sweep_deg",
            "root_twist_deg",
            "tip_twist_deg",
            "dihedral_deg",
            "tilt_deg",
            "x_position",
            "y_position",
            "z_position",
        }
    )

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

    @staticmethod
    def _number(element: ET.Element, tag: str) -> float:
        text = element.findtext(tag)
        if text is None:
            raise ConstraintError(f"The selected XFLR section has no {tag} field.")
        try:
            value = float(text)
        except ValueError as error:
            raise ConstraintError(f"The selected XFLR {tag} value is invalid: {text!r}.") from error
        if not isfinite(value):
            raise ConstraintError(f"The selected XFLR {tag} value must be finite.")
        return value

    @classmethod
    def planform_area(cls, xml_path: str | Path, component_name: str | None = None) -> float:
        """Return component planform area in native XML length units squared."""

        tree = ET.parse(xml_path)
        wing = cls._target_wing(tree.getroot(), component_name)
        sections = list(wing.findall("./Sections/Section"))
        if len(sections) < 2:
            raise ConstraintError("At least two sections are required to calculate planform area.")
        points = sorted(
            ((cls._number(section, "y_position"), cls._number(section, "Chord")) for section in sections),
            key=lambda point: point[0],
        )
        if any(chord <= 0 for _, chord in points):
            raise ConstraintError("Every section chord must be positive.")
        half_or_full_area = sum(
            abs(y1 - y0) * (chord0 + chord1) / 2.0
            for (y0, chord0), (y1, chord1) in zip(points, points[1:])
        )
        symmetric = (wing.findtext("Symetric") or "false").strip().lower() == "true"
        return 2.0 * half_or_full_area if symmetric else half_or_full_area

    @classmethod
    def geometry(cls, xml_path: str | Path, component_name: str | None = None) -> dict[str, float]:
        """Summarize geometry values useful to optimization reports."""

        tree = ET.parse(xml_path)
        wing = cls._target_wing(tree.getroot(), component_name)
        sections = list(wing.findall("./Sections/Section"))
        if not sections:
            raise ConstraintError("The wing has no sections.")
        first, last = sections[0], sections[-1]
        root_chord = cls._number(first, "Chord")
        tip_chord = cls._number(last, "Chord")
        return {
            "span": cls.span(xml_path, component_name),
            "planform_area": cls.planform_area(xml_path, component_name),
            "root_chord": root_chord,
            "tip_chord": tip_chord,
            "taper_ratio": tip_chord / root_chord,
            "tip_twist_deg": cls._number(last, "Twist"),
            "dihedral_deg": cls._number(first, "Dihedral"),
            "tilt_deg": float(wing.findtext("Tilt_angle") or 0.0),
        }

    @classmethod
    def apply_geometry(
        cls,
        xml_path: str | Path,
        parameters: Mapping[str, float],
        component_name: str | None = None,
    ) -> None:
        """Apply supported projected geometry parameters to a component XML.

        Supported keys are ``span``, ``chord_scale``, ``root_chord``,
        ``tip_chord``, ``taper_ratio``, ``sweep_deg`` (quarter-chord sweep),
        ``root_twist_deg``, ``tip_twist_deg``, ``dihedral_deg``, ``tilt_deg``,
        and the component ``x_position``/``y_position``/``z_position``.
        """

        unknown = set(parameters) - cls.SUPPORTED_GEOMETRY_PARAMETERS
        if unknown:
            raise ConstraintError(f"Unsupported geometry parameter(s): {', '.join(sorted(unknown))}.")
        converted = {name: float(value) for name, value in parameters.items()}
        if not all(isfinite(value) for value in converted.values()):
            raise ConstraintError("Geometry parameters must be finite.")
        for positive_key in ("span", "chord_scale", "root_chord", "tip_chord", "taper_ratio"):
            if positive_key in converted and converted[positive_key] <= 0:
                raise ConstraintError(f"{positive_key} must be positive.")

        # Span scaling is already unit-tested and handles symmetric/full span.
        if "span" in converted:
            cls.set_span(xml_path, converted.pop("span"), component_name)

        tree = ET.parse(xml_path)
        wing = cls._target_wing(tree.getroot(), component_name)
        sections = list(wing.findall("./Sections/Section"))
        if not sections:
            raise ConstraintError("The wing has no sections.")
        y_values = [cls._number(section, "y_position") for section in sections]
        root_y = y_values[0]
        spanwise_distances = [abs(y - root_y) for y in y_values]
        maximum_distance = max(spanwise_distances)
        if maximum_distance <= 0:
            raise ConstraintError("Cannot parameterize a component whose span is zero.")
        eta_values = [distance / maximum_distance for distance in spanwise_distances]

        chords = [cls._number(section, "Chord") for section in sections]
        if "chord_scale" in converted:
            scale = converted["chord_scale"]
            chords = [chord * scale for chord in chords]
        if "root_chord" in converted:
            root_scale = converted["root_chord"] / chords[0]
            chords = [chord * root_scale for chord in chords]
        if "taper_ratio" in converted:
            target_tip = chords[0] * converted["taper_ratio"]
            chords = [chords[0] + eta * (target_tip - chords[0]) for eta in eta_values]
        if "tip_chord" in converted:
            target_tip = converted["tip_chord"]
            chords = [chords[0] + eta * (target_tip - chords[0]) for eta in eta_values]
        if any(chord <= 0 for chord in chords):
            raise ConstraintError("Geometry changes produced a non-positive section chord.")
        for section, chord in zip(sections, chords):
            element = section.find("Chord")
            assert element is not None
            element.text = f"{chord:.6f}"

        if "sweep_deg" in converted:
            root_offset = cls._number(sections[0], "xOffset")
            sweep_tangent = tan(radians(converted["sweep_deg"]))
            root_quarter_chord = root_offset + 0.25 * chords[0]
            for section, distance, chord in zip(sections, spanwise_distances, chords):
                leading_edge = root_quarter_chord + sweep_tangent * distance - 0.25 * chord
                element = section.find("xOffset")
                if element is None:
                    raise ConstraintError("The selected XFLR section has no xOffset field.")
                element.text = f"{leading_edge:.6f}"

        if "root_twist_deg" in converted or "tip_twist_deg" in converted:
            root_twist = converted.get("root_twist_deg", cls._number(sections[0], "Twist"))
            tip_twist = converted.get("tip_twist_deg", cls._number(sections[-1], "Twist"))
            for section, eta in zip(sections, eta_values):
                element = section.find("Twist")
                if element is None:
                    raise ConstraintError("The selected XFLR section has no Twist field.")
                element.text = f"{root_twist + eta * (tip_twist - root_twist):.6f}"

        if "dihedral_deg" in converted:
            for section in sections:
                element = section.find("Dihedral")
                if element is None:
                    raise ConstraintError("The selected XFLR section has no Dihedral field.")
                element.text = f"{converted['dihedral_deg']:.6f}"

        if "tilt_deg" in converted:
            element = wing.find("Tilt_angle")
            if element is None:
                raise ConstraintError("The selected XFLR component has no Tilt_angle field.")
            element.text = f"{converted['tilt_deg']:.6f}"

        position_keys = ("x_position", "y_position", "z_position")
        if any(key in converted for key in position_keys):
            element = wing.find("Position")
            if element is None or element.text is None:
                raise ConstraintError("The selected XFLR component has no Position field.")
            try:
                position = [float(value.strip()) for value in element.text.split(",")]
            except ValueError as error:
                raise ConstraintError(f"Invalid XFLR Position value: {element.text!r}.") from error
            if len(position) != 3:
                raise ConstraintError(f"Invalid XFLR Position value: {element.text!r}.")
            for index, key in enumerate(position_keys):
                if key in converted:
                    position[index] = converted[key]
            element.text = ", ".join(f"{value:.6f}" for value in position)

        ET.indent(tree, space="    ")
        tree.write(xml_path, encoding="utf-8", xml_declaration=True)
