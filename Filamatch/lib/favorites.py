"""App-level Favorites material workflow for Filamatch.

Fusion 360's app-level *Favorites* library is the only place where a material and
its appearance can be colored for reuse across designs.  The verified behavior
encoded here (Fusion build 2706.1.17) is unusual:

* ``app.favoriteMaterials.add(material)`` accepts a material straight from a
  MaterialLibrary and returns a favorites copy whose **name and description are
  writable**.
* Each favorite material gets its own appearance clone, whose id starts with
  the material id.  The matching item in ``app.favoriteAppearances`` is colored
  (once).  Assigning ``favorite.appearance.color`` raises
  ``RuntimeError("InternalValidationError : appearances")`` **but still
  applies** the color, so it is only done when the material's appearance does
  not already read back the new colour, and that error is ignored.  After a
  restart ``favorite.appearance`` can be a foreign appearance; it is only
  trusted when its id starts with the material id (:func:`_own_appearance`).
* ``design.materials.addByCopy(favorite_material, name)`` copies the favorite
  *with* its corrected color into the design.  Favorites never update design
  materials in place, so an existing gray design material is migrated: copy
  the colored favorite in under a temporary name, relink the bodies, delete the
  old material and rename the copy (every step is undone on failure).
* The Material Browser swatch is never redrawn after an API colour change: every
  favorite shows the stock swatch of the material it was copied from.  New
  favorites are therefore copied from the :data:`SWATCH_TEMPLATES` material
  nearest in colour, and the catalog density replaces that template's density.
  Favorites cannot be removed through the API either, so once a new favorite
  exists an older Filamatch favorite with another template is renamed
  ``OLD - <name>``.

The module imports cleanly outside Fusion (no module-level ``adsk`` import) and
ships an executable fake-object self-test in ``__main__``.
"""

from __future__ import annotations

import logging
import math
import sys
import time
import traceback
import types
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterator, List, Optional, Sequence, Tuple

from . import fusion_helpers as _fh
from . import testing

logger = logging.getLogger("Filamatch." + __name__.rsplit(".", 1)[-1])

#: Prefix of the description written onto favorite and design materials.
MATERIAL_DESCRIPTION_PREFIX = "Filamatch | "
#: Description prefixes that mark favorites made by this add-in, including the
#: one written by earlier versions (FusionFilament Bridge).
_OWN_DESCRIPTION_PREFIXES = (MATERIAL_DESCRIPTION_PREFIX, "FusionFilament Bridge | ")

#: Maximum component nesting level (kept for compatibility; the body traversal
#: of :mod:`materials_workflow` applies its own limit).
MAX_COMPONENT_DEPTH = 8

#: Fallback category when a catalog entry does not provide one.
DEFAULT_FILAMENT_CATEGORY = "PLA"

#: Favorites material appearance color tolerance (verified read-back is exact;
#: one unit of slack covers colour-space rounding).
_COLOR_TOLERANCE = 1

#: Stock Fusion Material Library plastics whose stored swatch already shows a
#: colour, with that colour (verified in Fusion 2706.1.17).  Fusion never redraws
#: a swatch after an API colour change, so a favourite always shows the swatch of
#: the material it was copied from; the template nearest to the filament colour
#: makes the Material Browser swatch roughly match.  All use the Prism opaque or
#: transparent schema that :func:`_apply_appearance_color_properties` recolours.
SWATCH_TEMPLATES: Tuple[Tuple[str, str], ...] = (
    ("ABS Plastic", "#F5F5F3"),
    ("PC/ABS Plastic", "#B2B2B2"),
    ("Plastic, Opaque Black", "#191919"),
    ("Laminate, Blue, Matte", "#303B96"),
    ("Laminate, Red, Matte", "#C33426"),
    ("FR4", "#447766"),
    ("PAEK Plastic", "#E4C99D"),
    ("PEEK", "#99825E"),
    ("Phenolic Resin", "#F5B226"),
)

#: Name prefix given to replaced favorites (the API cannot remove favorites).
OLD_FAVORITE_PREFIX = "OLD - "

#: Words in a filament's colour/product name that keep its (clear) catalog template.
_CLEAR_FILAMENT_WORDS = ("clear", "transparent", "translucent")

#: Weight of the lightness difference in the swatch match; hue and chroma count
#: more, so a mid blue picks the blue swatch rather than the grey one.
_SWATCH_LIGHTNESS_WEIGHT = 0.5

#: Suffix of the temporary name a migrated design material is copied under.
_MIGRATION_SUFFIX = " (Filamatch update)"

#: Density difference (g/cm^3) at which a favorite's density is repaired.
_DENSITY_TOLERANCE = 0.005

__all__ = [
    "MATERIAL_DESCRIPTION_PREFIX",
    "MAX_COMPONENT_DEPTH",
    "DEFAULT_FILAMENT_CATEGORY",
    "SWATCH_TEMPLATES",
    "OLD_FAVORITE_PREFIX",
    "FavoritesError",
    "FavoriteFilamentResult",
    "color_hex",
    "nearest_swatch_template",
    "favorites_summary",
    "stale_swatch_favorites",
    "replace_stale_favorites",
    "replace_and_list_stale",
    "find_favorite",
    "ensure_favorite_filament",
    "import_catalog_to_favorites",
]


class FavoritesError(RuntimeError):
    """Raised when the Favorites material workflow cannot complete."""


@dataclass(frozen=True)
class FavoriteFilamentResult:
    """Outcome of importing one catalog filament into Favorites and the design."""

    catalog_id: str
    display_name: str
    color_hex: str
    favorite_material: Any
    favorite_appearance: Any
    design_material: Any
    replaced_existing: bool
    relinked_bodies: int


# ---------------------------------------------------------------------------
# Generic defensive helpers
# ---------------------------------------------------------------------------


_safe_get = _fh.safe_get
_display_name = _fh.display_name
_collection_items = _fh.collection_items
_item_by_name = _fh.item_by_name
_item_by_id = _fh.item_by_id
_entry_field = _fh.entry_field


def _require_text(value: Any, label: str) -> str:
    """Return a stripped non-empty string or raise FavoritesError."""
    if not isinstance(value, str) or not value.strip():
        raise FavoritesError("{} must be a non-empty string, got {!r}".format(label, value))
    return value.strip()


def _entry_category(entry: Any) -> str:
    """Return the entry category, falling back to its slicer filament type."""
    return _fh.entry_category(entry, DEFAULT_FILAMENT_CATEGORY)


# ---------------------------------------------------------------------------
# Color helpers
# ---------------------------------------------------------------------------


def _rgb_from_hex(color_hex_value: str) -> Tuple[int, int, int]:
    """Parse ``#RRGGBB`` into an ``(r, g, b)`` tuple, raising FavoritesError."""
    try:
        return _fh.rgb_from_hex(color_hex_value)
    except ValueError as exc:
        raise FavoritesError(str(exc)) from exc


def _normalized_hex(color_hex_value: str) -> str:
    """Return ``#RRGGBB`` uppercase for a validated color string."""
    red, green, blue = _rgb_from_hex(color_hex_value)
    return "#{:02X}{:02X}{:02X}".format(red, green, blue)


def color_hex(color: Any) -> str:
    """Convert a Fusion ``Color`` to an uppercase ``#RRGGBB`` string.

    Opacity is optional and ignored (Fusion colors expose ``.opacity`` and have
    no ``.alpha`` channel); only red/green/blue are read.

    Args:
        color: Object exposing ``red``, ``green`` and ``blue`` (0-255).

    Returns:
        The uppercase color string.

    Raises:
        FavoritesError: If the color or one of its channels is unusable.
    """
    try:
        return _fh.hex_from_color(color)
    except ValueError as exc:
        raise FavoritesError(str(exc)) from exc


def _colors_close(left: str, right: str, tolerance: int = _COLOR_TOLERANCE) -> bool:
    """Return whether two ``#RRGGBB`` colors differ by at most ``tolerance``."""
    try:
        left_rgb = _rgb_from_hex(left)
        right_rgb = _rgb_from_hex(right)
    except FavoritesError:
        return False
    return all(abs(a - b) <= tolerance for a, b in zip(left_rgb, right_rgb))


def _lab_from_hex(color_hex_value: str) -> Tuple[float, float, float]:
    """Convert ``#RRGGBB`` (sRGB, D65) to CIE L*a*b* (see printer_model.lab_from_hex)."""
    from . import printer_model

    red, green, blue = _rgb_from_hex(color_hex_value)  # raises FavoritesError on bad input
    return printer_model.lab_from_hex("#{:02X}{:02X}{:02X}".format(red, green, blue))


def nearest_swatch_template(color_hex_value: str) -> str:
    """Return the :data:`SWATCH_TEMPLATES` material whose swatch is nearest in colour.

    Args:
        color_hex_value: Filament colour as ``#RRGGBB``.

    Returns:
        The stock Fusion material name to copy the favorite from.

    Raises:
        FavoritesError: If the colour is not a ``#RRGGBB`` string.
    """
    lightness, a_axis, b_axis = _lab_from_hex(color_hex_value)

    def distance(template: Tuple[str, str]) -> float:
        other_lightness, other_a, other_b = _lab_from_hex(template[1])
        return math.sqrt(
            (_SWATCH_LIGHTNESS_WEIGHT * (lightness - other_lightness)) ** 2
            + (a_axis - other_a) ** 2
            + (b_axis - other_b) ** 2
        )

    return min(SWATCH_TEMPLATES, key=distance)[0]


def _new_color(color_hex_value: str) -> Any:
    """Build an opaque Fusion ``Color`` for ``color_hex_value``."""
    try:
        return _fh.new_color(color_hex_value)
    except (ValueError, RuntimeError) as exc:
        raise FavoritesError(str(exc)) from exc


# ---------------------------------------------------------------------------
# Descriptions and template resolution
# ---------------------------------------------------------------------------


def _build_description(entry: Any) -> str:
    """Build the material description carrying category/manufacturer metadata."""
    return _fh.entry_description(entry, MATERIAL_DESCRIPTION_PREFIX, DEFAULT_FILAMENT_CATEGORY)


def _set_description(obj: Any, description: str, label: str) -> None:
    """Write a description onto a Fusion object, logging (not raising) failures."""
    try:
        obj.description = description
        logger.debug("Set the description of %s to %r.", label, description)
    except Exception:
        logger.warning(
            "Could not set the description of %s (continuing): %r", label, description,
            exc_info=True,
        )


def _entry_color_hex(entry: Any) -> str:
    """Return the validated uppercase color of a catalog entry."""
    catalog_id = _entry_field(entry, "id")
    try:
        return _normalized_hex(_entry_field(entry, "color_hex"))
    except FavoritesError as exc:
        raise FavoritesError(
            "Catalog entry {!r} has an invalid color_hex: {}".format(catalog_id, exc)
        ) from exc


def _entry_density(entry: Any) -> Optional[float]:
    """Return the entry density in g/cm^3, or ``None`` when it is missing/invalid."""
    raw = _entry_field(entry, "density_g_cm3")
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    value = float(raw)
    return value if math.isfinite(value) and value > 0 else None


def _swatch_template_name(entry: Any, catalog_template: str) -> Optional[str]:
    """Return the colour-matched template for ``entry``, or ``None`` to keep the catalog one.

    The swatch template's physical properties replace the catalog template's, so
    it is only used when the entry carries its own density (written onto the
    favorite afterwards).  Clear filaments keep their clear catalog template.
    """
    if _entry_density(entry) is None:
        return None
    words = " ".join(
        str(_entry_field(entry, field, "") or "")
        for field in ("color_name", "product_line", "display_name")
    ).casefold()
    if any(word in words for word in _CLEAR_FILAMENT_WORDS):
        return None
    try:
        name = nearest_swatch_template(_entry_color_hex(entry))
    except FavoritesError:
        return None
    return None if name == catalog_template else name


def _set_density(material: Any, entry: Any, label: str) -> None:
    """Write the entry density onto a material, logging (not raising) failures."""
    density = _entry_density(entry)
    if density is None:
        return
    try:
        from . import importer

        importer.set_material_density(material, density)
    except Exception:
        logger.warning(
            "Could not set the density of %s to %s g/cm^3 (continuing).",
            label,
            density,
            exc_info=True,
        )


#: Names the solid values in a filament material's description.
SOLID_VALUES_NOTE = "solid values: typical print, "
#: Says Filamatch wrote none (the printed values look for "solid values:", so this must not contain it).
NO_SOLID_VALUES_NOTE = "no solid values ({})"


def solid_values(entry: Any) -> Optional[Tuple[Dict[str, float], str]]:
    """The filament's solid stiffness and strength (MPa, ``material_copy`` keys) and their basis.

    Its own data sheet when Filamatch has it, else its type's generic values
    (``strength_model``), as a typical print at 100% infill: between along and
    across the layers. ``None`` when there is no data for the filament, and
    for a flexible one (a linear simulation does not fit it).
    """
    try:
        from . import strength_model

        data = strength_model.load()
    except Exception:
        logger.warning("Could not read the stiffness and strength data (continuing).", exc_info=True)
        return None
    if _is_flexible(entry):
        return None
    maker = str(_entry_field(entry, "manufacturer", "") or "").strip()
    product_line = str(_entry_field(entry, "product_line", "") or "").strip()
    basis = data.product_for(["{} {}".format(maker, product_line)])
    if basis is None:
        for field in ("slicer_filament_type", "category"):
            found = data.match(str(_entry_field(entry, field, "") or ""))
            if found:
                basis = data.types.get(found)
                break
    if basis is None:
        return None
    e_mpa, yield_mpa, tensile_mpa, _notes = strength_model.base_values(basis, "typical")
    poisson = float(basis.poisson)
    values = {
        "e_mpa": e_mpa,
        "poisson": poisson,
        "shear_mpa": e_mpa / (2.0 * (1.0 + poisson)),
        "yield_mpa": yield_mpa,
        "tensile_mpa": tensile_mpa,
    }
    return values, basis.label


def _is_flexible(entry: Any) -> bool:
    """True for a flexible filament (TPU, TPE, ...), by its type or name."""
    try:
        from . import strength_model
    except Exception:
        return False
    return strength_model.is_flexible(*(str(_entry_field(entry, field, "") or "")
                                        for field in ("slicer_filament_type", "category", "product_line")))


def _set_solid_values(material: Any, entry: Any, description: str, label: str) -> None:
    """Write the filament's solid stiffness and strength onto a material, best effort.

    The colour-matched template brings its own mechanical values (a blue one
    made an ABS 6.9 GPa stiff), and Fusion Simulation uses them. The
    description names what replaced them. A filament without data, or a
    flexible one, keeps the values it has, and its description says so.
    """
    if getattr(material, "materialProperties", None) is None:
        return
    solid = solid_values(entry)
    if solid is None:
        reason = "flexible filament" if _is_flexible(entry) else "no data for this filament"
        _set_description(material, "{} | {}".format(description, NO_SOLID_VALUES_NOTE.format(reason)), label)
        return
    values, basis = solid
    try:
        from . import material_copy

        left_out = material_copy.write_values(material, values, None)
    except Exception:
        logger.warning("Could not write the solid values of %s (continuing).", label, exc_info=True)
        return
    if left_out:
        logger.info("Solid values of %s: %s", label, left_out)
    _set_description(material, "{} | {}{}".format(description, SOLID_VALUES_NOTE, basis), label)


def _resolve_template(
    app: Any, design: Any, entry: Any, template_material: Any
) -> Any:
    """Return the template material to favorite, resolving it via the importer.

    The colour-matched :data:`SWATCH_TEMPLATES` material is preferred so the
    favorite's swatch shows a similar colour; the catalog ``template_material``
    is the fallback.
    """
    if template_material is not None:
        return template_material
    catalog_id = _entry_field(entry, "id")
    template_name = _entry_field(entry, "template_material")
    if not isinstance(template_name, str) or not template_name.strip():
        raise FavoritesError(
            "Catalog entry {!r} has no template_material; add it to "
            "catalog/filaments.json or pass template_material explicitly.".format(catalog_id)
        )
    template_name = template_name.strip()
    try:
        from . import importer
    except ImportError as exc:
        logger.exception("The importer module is unavailable.")
        raise FavoritesError(
            "The importer module (importer.py) is unavailable, so template material "
            "{!r} cannot be resolved. Pass template_material explicitly or restore "
            "importer.py. Details: {}".format(template_name, exc)
        ) from exc
    swatch_name = _swatch_template_name(entry, template_name)
    if swatch_name is not None:
        try:
            resolved = importer.find_template_material(design, swatch_name, app)
        except Exception:
            logger.warning(
                "Swatch template %r is unavailable for %r; using %r.",
                swatch_name,
                catalog_id,
                template_name,
                exc_info=True,
            )
        else:
            if resolved is not None:
                logger.debug(
                    "Resolved swatch template %r for catalog entry %r.", swatch_name, catalog_id
                )
                return resolved
    try:
        resolved = importer.find_template_material(design, template_name, app)
    except Exception as exc:
        logger.exception("Could not resolve template material %r.", template_name)
        raise FavoritesError(
            "Template material {!r} was not found for catalog entry {!r}. Fix: load the "
            "Fusion 360 Material Library (or the manufacturer .adsklib) in Fusion, or "
            "change 'template_material' for this entry in catalog/filaments.json. "
            "Details: {}".format(template_name, catalog_id, exc)
        ) from exc
    if resolved is None:
        raise FavoritesError(
            "Template material {!r} resolved to null for catalog entry {!r}; load the "
            "Fusion 360 Material Library or change 'template_material' in "
            "catalog/filaments.json.".format(template_name, catalog_id)
        )
    logger.debug("Resolved template material %r for catalog entry %r.", template_name, catalog_id)
    return resolved


# ---------------------------------------------------------------------------
# Favorite materials / appearances
# ---------------------------------------------------------------------------


def _ensure_favorite_material(
    app: Any,
    catalog_id: str,
    template_material: Any,
    display_name: Optional[str] = None,
    favorites: Optional[List[Any]] = None,
) -> Any:
    """Return the existing favorite material or add/rename a new one.

    The favorite is named with the standard ``Brand - Material - Color`` display
    name (not the underscore catalog id) so the Favorites list and the generated
    custom material library present readable names.  Old Filamatch favorites
    with another template are renamed ``OLD - <name>`` only after the new
    favorite was added, so a failed add leaves every name unchanged.

    ``favorites`` is an already enumerated Favorites list (one walk shared by a
    whole batch); a newly added favorite is appended to it.
    """
    favorite_materials = _safe_get(app, "favoriteMaterials", "the Fusion application")
    if favorite_materials is None:
        raise FavoritesError(
            "The Fusion application exposes no favoriteMaterials collection; Favorites "
            "are unavailable in this Fusion version."
        )
    if favorites is None:
        favorites = _collection_items(favorite_materials, "favorite materials")
    target_name = str(display_name or "").strip() or catalog_id
    wanted = _appearance_name(template_material)
    favorite, retired = _reusable_favorite(favorites, (catalog_id, target_name), wanted)
    if favorite is not None:
        logger.debug("Reusing existing favorite material %r.", target_name)
    else:
        logger.info("Adding template material to Favorites for catalog entry %r.", catalog_id)
        try:
            favorite = favorite_materials.add(template_material)
        except Exception as exc:
            logger.exception("app.favoriteMaterials.add failed for %r.", catalog_id)
            raise FavoritesError(
                "Fusion could not add a favorite material for {!r}: {}".format(
                    catalog_id, exc
                )
            ) from exc
        if favorite is None:
            raise FavoritesError(
                "Fusion returned null when adding a favorite material for {!r}".format(
                    catalog_id
                )
            )
        favorites.append(favorite)
        _retire_old_favorites(retired, wanted)
    if _display_name(favorite, "name", "a favorite material", "") != target_name:
        try:
            favorite.name = target_name
        except Exception:
            logger.warning(
                "Could not rename the favorite material to %r (continuing with its current "
                "Fusion name).",
                target_name,
                exc_info=True,
            )
        else:
            logger.info("Renamed favorite material to %r.", target_name)
    return favorite


def _appearance_name(material: Any) -> str:
    """Return the name of a material's appearance (its template look), or ``""``."""
    return _display_name(
        _safe_get(material, "appearance", "a material"), "name", "an appearance", ""
    )


def _reusable_favorite(
    favorites: Sequence[Any], names: Sequence[str], wanted: str
) -> Tuple[Any, List[Any]]:
    """Return ``(reusable favorite or None, old favorites to retire)``.

    A Filamatch favorite whose appearance is not the wanted template's (an old
    white ``ABS (White)`` copy) cannot be recoloured or removed through the
    API: it is returned for :func:`_retire_old_favorites` so a new favorite with
    the right swatch takes its name.  Favorites the user made themselves are
    reused unchanged.
    """
    matches = [
        material
        for material in favorites
        if _display_name(material, "name", "a favorite material", "") in names
    ]
    for material in matches:
        appearance = _own_appearance(material)
        if not wanted or appearance is None or _display_name(
            appearance, "name", "an appearance", ""
        ) == wanted:
            # An unreadable appearance (see _own_appearance) is reused, never replaced.
            return material, []
    for material in matches:
        description = _display_name(material, "description", "a favorite material", "")
        if not description.startswith(_OWN_DESCRIPTION_PREFIXES):
            return material, []
    return None, matches


def _retire_old_favorites(retired: Sequence[Any], wanted: str) -> None:
    """Rename replaced favorites :data:`OLD_FAVORITE_PREFIX` + name (best effort)."""
    for material in retired:
        old_name = _display_name(material, "name", "a favorite material", "")
        try:
            material.name = OLD_FAVORITE_PREFIX + old_name
        except Exception:
            logger.warning("Could not rename old favorite %r.", old_name, exc_info=True)
            continue
        logger.info(
            "Renamed old favorite %r (swatch %r) to %r; the new one has the %r swatch.",
            old_name,
            _appearance_name(material),
            OLD_FAVORITE_PREFIX + old_name,
            wanted,
        )


def _apply_appearance_color_properties(appearance: Any, normalized_hex: str) -> None:
    """Set the color-bearing appearance properties of a design appearance.

    Fusion's ``Appearance.color`` setter only updates ``opaque_albedo``.  The
    material preview also uses ``surface_albedo`` (which stays white for copied
    template appearances), and transparent schemas use ``transparent_color``.
    Every write is best-effort.
    """
    _fh.set_color_slots(
        appearance,
        _new_color(normalized_hex),
        "an appearance",
        ("opaque_albedo", "surface_albedo", "transparent_color"),
    )


def _clear_color_texture(appearance: Any) -> None:
    """Disconnect any base-color texture so the solid appearance color shows."""
    _fh.clear_color_texture(appearance)


def _favorite_appearance_item(favorite_appearances: Any, favorite: Any) -> Tuple[Any, Any]:
    """Return ``(FavoriteAppearances item, trusted material appearance or None)``.

    The item is the favorite's own appearance: found by the id of
    ``favorite.appearance`` when that can be trusted (:func:`_own_appearance`),
    else by scanning for the appearance whose id starts with the material id.
    ``(None, ...)`` means the favorite's appearance cannot be identified.
    """
    own = _own_appearance(favorite)
    material_id = str(_safe_get(favorite, "id", "a favorite material") or "")
    if own is not None:
        appearance_id = str(_safe_get(own, "id", "a favorite appearance") or "")
        item = _item_by_id(favorite_appearances, appearance_id, "favorite appearances")
        if item is not None:
            return item, own
    if material_id:
        for item in _collection_items(favorite_appearances, "favorite appearances"):
            item_id = str(_safe_get(item, "id", "a favorite appearance") or "")
            if item_id.startswith(material_id):
                return item, own
    return None, own


def _color_favorite_appearance(
    app: Any, favorite: Any, catalog_id: str, normalized_hex: str
) -> Any:
    """Color the favorite's own FavoriteAppearances item; return that item.

    Only the favorite's own appearance is touched: after a restart
    ``favorite.appearance`` can be a foreign appearance, which is never
    recoloured.  The colour is written once; an item that already carries it is
    left alone.

    Raises:
        FavoritesError: If the own appearance cannot be found, colored or read back.
    """
    favorite_appearances = _safe_get(app, "favoriteAppearances", "the Fusion application")
    if favorite_appearances is None:
        raise FavoritesError(
            "The Fusion application exposes no favoriteAppearances collection; the "
            "favorite for {!r} cannot be colored.".format(catalog_id)
        )
    favorite_appearance, own = _favorite_appearance_item(favorite_appearances, favorite)
    if favorite_appearance is None:
        raise FavoritesError(
            "Could not find the own appearance of favorite material {!r} in "
            "FavoriteAppearances, so nothing was recoloured; remove the favorite from "
            "the Favorites library and import it again.".format(catalog_id)
        )

    current = _fh.appearance_hex(favorite_appearance)
    if current is None or not _colors_close(current, normalized_hex):
        color = _new_color(normalized_hex)
        try:
            favorite_appearance.color = color
        except Exception as exc:
            logger.exception("Could not set the color of favorite appearance %r.", catalog_id)
            raise FavoritesError(
                "Could not set the color of the favorite appearance for {!r}: {}".format(
                    catalog_id, exc
                )
            ) from exc
        _clear_color_texture(favorite_appearance)
        # ``color`` wrote opaque_albedo; the other slots once each.
        _fh.set_color_slots(
            favorite_appearance,
            color,
            "favorite appearance {!r}".format(catalog_id),
            ("surface_albedo", "transparent_color"),
        )

    if own is not None and own is not favorite_appearance:
        own_hex = _fh.appearance_hex(own)
        if own_hex is None or not _colors_close(own_hex, normalized_hex):
            try:
                own.color = _new_color(normalized_hex)
            except Exception:
                # Verified Fusion behavior: the proxy assignment raises
                # "InternalValidationError : appearances" but still applies the color.
                logger.debug(
                    "Ignoring the known favorite-material appearance color proxy error "
                    "for %r (the color is still applied).",
                    catalog_id,
                    exc_info=True,
                )

    try:
        actual_hex = color_hex(_safe_get(favorite_appearance, "color", "favorite appearance"))
    except FavoritesError as exc:
        raise FavoritesError(
            "Could not read back the favorite appearance color for {!r}: {}".format(
                catalog_id, exc
            )
        ) from exc
    if not _colors_close(actual_hex, normalized_hex):
        raise FavoritesError(
            "Favorite appearance for {!r} read back as {} instead of {}; remove the "
            "favorite from the Favorites library and retry.".format(
                catalog_id, actual_hex, normalized_hex
            )
        )
    logger.info("Colored favorite appearance for %r as %s.", catalog_id, actual_hex)
    return favorite_appearance


# ---------------------------------------------------------------------------
# Design material synchronisation
# ---------------------------------------------------------------------------


def _design_material_has_color(design_material: Any, normalized_hex: str) -> bool:
    """Return whether a design material's appearance already matches a color."""
    appearance = _safe_get(design_material, "appearance", "a design material")
    if appearance is None:
        return False
    color = _safe_get(appearance, "color", "a design material appearance")
    if color is None:
        return False
    try:
        actual_hex = color_hex(color)
    except FavoritesError:
        logger.warning("Could not read a design material appearance color.", exc_info=True)
        return False
    return _colors_close(actual_hex, normalized_hex)


def _design_material_matches(design_material: Any, favorite: Any, normalized_hex: str) -> bool:
    """Return whether a design material can stay: same colour and same template look.

    A copy of an older favorite keeps that favorite's template (and so its white
    ``ABS (White)`` swatch); it is replaced by a copy of the new favorite.  The
    favorite's template is compared only when its appearance can be trusted.
    """
    if not _design_material_has_color(design_material, normalized_hex):
        return False
    favorite_appearance = _own_appearance(favorite)
    if favorite_appearance is None:
        return True
    wanted = _display_name(favorite_appearance, "name", "a favorite appearance", "")
    return not wanted or _appearance_name(design_material) == wanted


def _rename(obj: Any, name: str, label: str) -> None:
    """Rename a Fusion object, logging (not raising) failures."""
    try:
        obj.name = name
    except Exception:
        logger.warning("Could not rename a %s to %r.", label, name, exc_info=True)


def _same_material(candidate: Any, target: Any, catalog_id: str) -> bool:
    """Return whether ``candidate`` is the design material ``target``.

    Matched by name: copies of one favorite share the favorite's material id
    (verified in Fusion 2706.1.17), so an id match can be a different material.
    """
    if candidate is None or target is None:
        return False
    if candidate is target:
        return True
    candidate_name = _display_name(candidate, "name", "a body material", "")
    target_name = _display_name(target, "name", "a design material", "")
    return bool(candidate_name) and candidate_name == target_name


def _iter_design_bodies(design: Any) -> Iterator[Any]:
    """Yield every body in the design (traversal of :mod:`materials_workflow`)."""
    from . import materials_workflow

    for body, _path in materials_workflow._iter_design_bodies(design):
        yield body


def _find_bodies_using_material(
    design: Any, existing_material: Any, catalog_id: str
) -> List[Any]:
    """Return every body in the design that currently uses ``existing_material``.

    A component used by several occurrences yields its native body once per
    occurrence; each body is returned once.
    """
    bodies: List[Any] = []
    seen = _fh.BodySet()
    for body in _iter_design_bodies(design):
        material = _safe_get(body, "material", "a design body")
        if _same_material(material, existing_material, catalog_id) and seen.add(body):
            bodies.append(body)
    return bodies


def _copy_favorite_into_design(design_materials: Any, favorite: Any, catalog_id: str) -> Any:
    """Copy a favorite material into the design, raising FavoritesError on failure."""
    try:
        design_material = design_materials.addByCopy(favorite, catalog_id)
    except Exception as exc:
        logger.exception("design.materials.addByCopy failed for %r.", catalog_id)
        raise FavoritesError(
            "Could not copy favorite material {!r} into the design: {}".format(
                catalog_id, exc
            )
        ) from exc
    if design_material is None:
        raise FavoritesError(
            "Fusion returned null when copying favorite material {!r} into the design".format(
                catalog_id
            )
        )
    return design_material


def _free_temporary_name(design_materials: Any, base: str) -> str:
    """Return ``base`` + :data:`_MIGRATION_SUFFIX` (numbered while taken)."""
    candidate = base + _MIGRATION_SUFFIX
    counter = 2
    while _item_by_name(design_materials, candidate, "design materials") is not None:
        candidate = "{}{} {}".format(base, _MIGRATION_SUFFIX, counter)
        counter += 1
    return candidate


def _delete_quietly(material: Any, label: str) -> None:
    """Delete a design material, logging (not raising) failures."""
    try:
        material.deleteMe()
    except Exception:
        logger.warning("Could not delete the %s.", label, exc_info=True)


def _relink(bodies: Sequence[Any], material: Any, catalog_id: str) -> None:
    """Best-effort relink of ``bodies`` to ``material`` (used to undo a migration)."""
    for body in bodies:
        try:
            body.material = material
        except Exception:
            logger.exception("Could not relink a body back to design material %r.", catalog_id)


def _migrate_design_material(
    design: Any,
    design_materials: Any,
    existing_material: Any,
    favorite: Any,
    catalog_id: str,
) -> Tuple[Any, int]:
    """Replace an existing (gray) design material with the colored favorite copy.

    The favorite is copied in under a temporary name, the bodies are relinked to
    the copy, the old material is deleted and the copy takes ``catalog_id`` as
    its name.  A failed relink or delete moves the bodies back and removes the
    copy, so the design is never left without the old material.
    """
    bodies = _find_bodies_using_material(design, existing_material, catalog_id)
    temporary_name = _free_temporary_name(design_materials, catalog_id)
    design_material = _copy_favorite_into_design(design_materials, favorite, temporary_name)
    if design_material is existing_material or _same_material(
        design_material, existing_material, catalog_id
    ):
        raise FavoritesError(
            "Fusion returned the existing design material {!r} instead of a new copy; "
            "nothing was changed.".format(catalog_id)
        )

    moved: List[Any] = []
    for body in bodies:
        try:
            body.material = design_material
        except Exception as exc:
            logger.exception("Could not relink a body to the new design material %r.", catalog_id)
            _relink(moved, existing_material, catalog_id)
            _delete_quietly(design_material, "unused copy of {!r}".format(catalog_id))
            raise FavoritesError(
                "Could not relink a body to the new copy of design material {!r}; the "
                "design was left unchanged. Details: {}".format(catalog_id, exc)
            ) from exc
        moved.append(body)

    try:
        existing_material.deleteMe()
    except Exception as exc:
        logger.exception("Could not delete existing design material %r.", catalog_id)
        _relink(moved, existing_material, catalog_id)
        _delete_quietly(design_material, "unused copy of {!r}".format(catalog_id))
        raise FavoritesError(
            "Fusion could not delete the existing design material {!r} because it is "
            "still in use; detach it from every body in the Material Browser and retry. "
            "Details: {}".format(catalog_id, exc)
        ) from exc
    _rename(design_material, catalog_id, "design material")
    logger.info(
        "Replaced design material %r with the colored favorite copy and relinked %d body(ies).",
        catalog_id,
        len(bodies),
    )
    return design_material, len(bodies)


def _warn_on_design_material_color(
    design_material: Any, catalog_id: str, normalized_hex: str
) -> None:
    """Log a warning when a copied design material does not carry the catalog color."""
    appearance = _safe_get(design_material, "appearance", "the new design material")
    if appearance is None:
        logger.warning("The new design material for %r has no appearance to inspect.", catalog_id)
        return
    color = _safe_get(appearance, "color", "the new design material appearance")
    if color is None:
        logger.warning("The new design material for %r has no appearance color.", catalog_id)
        return
    try:
        actual_hex = color_hex(color)
    except FavoritesError:
        logger.warning("Could not read the new design material color for %r.", catalog_id)
        return
    if not _colors_close(actual_hex, normalized_hex):
        logger.warning(
            "The new design material for %r reads back as %s instead of %s.",
            catalog_id,
            actual_hex,
            normalized_hex,
        )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def favorites_summary(app: Any) -> Dict[str, Any]:
    """Return a summary of the application-level Favorites materials.

    Args:
        app: Fusion ``Application`` exposing ``favoriteMaterials``.

    Returns:
        ``{"count": int, "names": [str, ...]}`` where names keep Fusion's order.

    Raises:
        FavoritesError: If ``app`` is ``None`` or the collection is inaccessible.
    """
    if app is None:
        raise FavoritesError("favorites_summary requires the Fusion Application, got None")
    favorite_materials = _safe_get(app, "favoriteMaterials", "the Fusion application")
    if favorite_materials is None:
        return {"count": 0, "names": []}
    items = _collection_items(favorite_materials, "favorite materials")
    names = [
        _display_name(material, "name", "a favorite material", "")
        for material in items
    ]
    return {"count": len(items), "names": [name for name in names if name]}


def _own_appearance(material: Any) -> Any:
    """Return the favorite's own appearance, or ``None`` when it cannot be trusted.

    Verified in Fusion 2706.1.17: right after Fusion starts, a favorite that was
    not used yet in the session reports an unrelated appearance (the active
    design's ``Steel - Satin``).  A favorite's own appearance id starts with its
    material id (``<material id>_physmat_aspects...``).
    """
    appearance = _safe_get(material, "appearance", "a favorite material")
    material_id = str(_safe_get(material, "id", "a favorite material") or "")
    appearance_id = str(_safe_get(appearance, "id", "a favorite appearance") or "")
    if material_id and appearance_id.startswith(material_id):
        return appearance
    return None


def _catalog_values(entries: Optional[Sequence[Any]]) -> Dict[str, Tuple[str, Optional[float]]]:
    """Map catalog display names to ``(#RRGGBB, density)`` (first entry wins)."""
    values: Dict[str, Tuple[str, Optional[float]]] = {}
    for entry in entries or ():
        name = _display_name(entry, "display_name", "catalog entry", "")
        if not name or name in values:
            continue
        try:
            values[name] = (_entry_color_hex(entry), _entry_density(entry))
        except FavoritesError:
            continue
    return values


def _scan_favorites(
    app: Any, entries: Optional[Sequence[Any]], favorites: Sequence[Any]
) -> Tuple[
    List[Tuple[Any, str, Any, str, Optional[float]]],
    List[Tuple[Any, str, str, Optional[float]]],
]:
    """Walk Filamatch favorites once; return ``(stale, drifted)``.

    ``stale`` holds ``(favorite, name, template, colour, density)`` for
    favorites copied from a template that does not follow their colour.  The
    colour comes from the catalog entry of the same name, else from the
    favorite's own appearance.  ``drifted`` holds ``(favorite, name, colour,
    density)`` for catalog favorites with the right template whose colour or
    density differs from the catalog.  Favorites whose appearance cannot be
    trusted are skipped (never reported or changed on a bad read).
    """
    try:
        from . import importer
    except ImportError:
        logger.debug("importer is unavailable; favorites are not checked.")
        return [], []
    catalog = _catalog_values(entries)
    templates: Dict[str, Any] = {}
    stale: List[Tuple[Any, str, Any, str, Optional[float]]] = []
    drifted: List[Tuple[Any, str, str, Optional[float]]] = []
    for material in favorites:
        description = _display_name(material, "description", "a favorite material", "")
        if not description.startswith(_OWN_DESCRIPTION_PREFIXES):
            continue
        name = _display_name(material, "name", "a favorite material", "")
        appearance = _own_appearance(material)
        if appearance is None:
            logger.debug("Favorite %r reports a foreign appearance; not checked.", name)
            continue
        is_clear = any(word in name.casefold() for word in _CLEAR_FILAMENT_WORDS)
        catalog_name = name[len(OLD_FAVORITE_PREFIX):] if name.startswith(OLD_FAVORITE_PREFIX) else name
        try:
            actual_hex = color_hex(_safe_get(appearance, "color", "a favorite appearance"))
        except FavoritesError:
            actual_hex = ""
        is_stale = False
        if not is_clear:
            try:
                if catalog_name in catalog:
                    normalized_hex, density = catalog[catalog_name]
                elif actual_hex:
                    normalized_hex = actual_hex
                    density = importer.get_material_density(material)
                else:
                    continue
                wanted = nearest_swatch_template(normalized_hex)
            except FavoritesError:
                continue
            if wanted not in templates:
                try:
                    templates[wanted] = importer.find_template_material(None, wanted, app)
                except Exception:
                    logger.debug("Swatch template %r is unavailable.", wanted, exc_info=True)
                    templates[wanted] = None
            wanted_appearance = _appearance_name(templates[wanted])
            current = _display_name(appearance, "name", "an appearance", "")
            if wanted_appearance and current and current != wanted_appearance:
                stale.append((material, name, templates[wanted], normalized_hex, density))
                is_stale = True
        if is_stale or name not in catalog:
            continue
        wanted_hex, wanted_density = catalog[name]
        color_drift = not actual_hex or not _colors_close(actual_hex, wanted_hex)
        density_drift = False
        if wanted_density is not None:
            try:
                current_density = importer.get_material_density(material)
            except Exception:
                current_density = None
            density_drift = (
                current_density is None
                or abs(current_density - wanted_density) > _DENSITY_TOLERANCE
            )
        if color_drift or density_drift:
            drifted.append(
                (material, name, wanted_hex if color_drift else "", wanted_density if density_drift else None)
            )
    return stale, drifted


def _stale_favorites(
    app: Any, entries: Optional[Sequence[Any]] = None, favorites: Optional[Sequence[Any]] = None
) -> List[Tuple[Any, str, Any, str, Optional[float]]]:
    """Return ``(favorite, name, template, colour, density)`` for stale Filamatch favorites."""
    if favorites is None:
        favorites = _collection_items(
            _safe_get(app, "favoriteMaterials", "the Fusion application"), "favorite materials"
        )
    return _scan_favorites(app, entries, favorites)[0]


def stale_swatch_favorites(app: Any, entries: Optional[Sequence[Any]] = None) -> List[str]:
    """Return Filamatch favorites whose swatch does not follow their colour.

    They were copied from an older template (usually the white ``ABS (White)``).
    The API can neither remove a favorite nor redraw its swatch:
    :func:`replace_stale_favorites` renames them ``OLD - <name>`` and the user
    removes those by hand.

    Args:
        app: Fusion ``Application`` exposing ``favoriteMaterials``.
        entries: Catalog entries; their colours win over the favorites' own.

    Returns:
        Favorite names in Fusion's order; empty when nothing can be checked.
    """
    return [item[1] for item in _stale_favorites(app, entries)]


def replace_stale_favorites(app: Any, entries: Optional[Sequence[Any]] = None) -> List[str]:
    """Replace every stale Filamatch favorite with a colour-template copy.

    Each old favorite is renamed ``OLD - <name>`` and a new favorite with the
    same name and description, the catalog (else its own) colour and density is
    made from the nearest :data:`SWATCH_TEMPLATES` material.  Catalog favorites
    with the right template but a drifted colour or density are repaired in
    place.  Nothing is added to any design.

    Args:
        app: Fusion ``Application`` exposing the Favorites collections.
        entries: Catalog entries; their colour and density are used by name.

    Returns:
        The names that got a new favorite.  Failures are logged and skipped.
    """
    return replace_and_list_stale(app, entries)[0]


def replace_and_list_stale(
    app: Any, entries: Optional[Sequence[Any]] = None
) -> Tuple[List[str], List[str]]:
    """Like :func:`replace_stale_favorites`, also returning the still-stale names.

    The second list is what :func:`stale_swatch_favorites` would report right
    after the replacement (replaced favorites as ``OLD - <name>``), computed
    from the same single walk over the Favorites instead of a second one.
    """
    started = time.monotonic()
    with _fh.timed(logger, "Favorites scan"):
        favorites = _collection_items(
            _safe_get(app, "favoriteMaterials", "the Fusion application"), "favorite materials"
        )
        stale, drifted = _scan_favorites(app, entries, favorites)
    replaced: List[str] = []
    for material, name, template, normalized_hex, density in stale:
        if name.startswith(OLD_FAVORITE_PREFIX) or name in replaced:
            continue
        try:
            with _fh.timed(logger, "Replacing favorite {!r}".format(name)):
                description = _display_name(material, "description", "a favorite material", "")
                favorite = _ensure_favorite_material(
                    app, name, template, display_name=name, favorites=favorites
                )
                _set_description(favorite, description, "favorite material {!r}".format(name))
                _set_density(
                    favorite,
                    types.SimpleNamespace(density_g_cm3=density),
                    "favorite material {!r}".format(name),
                )
                _color_favorite_appearance(app, favorite, name, normalized_hex)
        except Exception:
            logger.exception("Could not replace the old favorite %r.", name)
            continue
        replaced.append(name)
    if replaced:
        logger.info("Replaced %d old favorite(s): %s", len(replaced), ", ".join(replaced))
    if drifted:
        with _fh.timed(logger, "Repairing favorite values"):
            _repair_favorite_values(app, drifted)
    still_stale = [
        OLD_FAVORITE_PREFIX + name if name in replaced else name
        for _material, name, _template, _hex, _density in stale
    ]
    logger.info(
        "Favorite replacement finished in %.2f s (%d favorite(s), %d replaced, %d repaired).",
        time.monotonic() - started,
        len(favorites),
        len(replaced),
        len(drifted),
    )
    return replaced, still_stale


def _repair_favorite_values(
    app: Any, drifted: Sequence[Tuple[Any, str, str, Optional[float]]]
) -> List[str]:
    """Write the catalog colour and/or density onto favorites that drifted.

    Covers favorites whose template is right but whose values are not (made
    from a bad appearance read, see :func:`_own_appearance`).  ``drifted`` comes
    from :func:`_scan_favorites`; an empty colour or ``None`` density means
    that value is already right.
    """
    repaired: List[str] = []
    for material, name, wanted_hex, wanted_density in drifted:
        try:
            if wanted_hex:
                _color_favorite_appearance(app, material, name, wanted_hex)
            if wanted_density is not None:
                _set_density(
                    material,
                    types.SimpleNamespace(density_g_cm3=wanted_density),
                    "favorite material {!r}".format(name),
                )
        except Exception:
            logger.exception("Could not repair the values of favorite %r.", name)
            continue
        repaired.append(name)
    if repaired:
        logger.info("Repaired colour/density of %d favorite(s): %s", len(repaired), ", ".join(repaired))
    return repaired


def find_favorite(app: Any, name: str) -> Any:
    """Return the favorite material named ``name``, or ``None``.

    Args:
        app: Fusion ``Application`` exposing ``favoriteMaterials``.
        name: Favorite material name (usually the catalog id).

    Returns:
        The favorite material, or ``None`` when it does not exist.

    Raises:
        FavoritesError: If ``app`` or ``name`` is invalid, or the collection is
            unavailable.
    """
    if app is None:
        raise FavoritesError("find_favorite requires the Fusion Application, got None")
    favorite_name = _require_text(name, "favorite material name")
    favorite_materials = _safe_get(app, "favoriteMaterials", "the Fusion application")
    if favorite_materials is None:
        raise FavoritesError(
            "The Fusion application exposes no favoriteMaterials collection; Favorites "
            "are unavailable in this Fusion version."
        )
    return _item_by_name(favorite_materials, favorite_name, "favorite materials")


def ensure_favorite_filament(
    app: Any, design: Any, entry: Any, template_material: Any = None
) -> FavoriteFilamentResult:
    """Create/reuse the favorite filament and sync it into the design.

    The favorite material is created from ``template_material`` (or resolved via
    :func:`importer.find_template_material`), renamed to the catalog id, given
    its description, and colored through the FavoriteAppearances collection
    (ignoring the verified proxy-color RuntimeError).  The design then receives
    a copy of the favorite; if a design material with the same name already
    exists with a different color it is replaced atomically by migrating its
    bodies.

    Args:
        app: Fusion ``Application`` exposing ``favoriteMaterials`` and
            ``favoriteAppearances``.
        design: Active Fusion design exposing ``materials`` and
            ``rootComponent``.
        entry: Catalog entry (attribute or mapping based) with at least ``id``,
            ``color_hex`` and ``template_material``.
        template_material: Optional explicit template material; resolved through
            the importer module when omitted.

    Returns:
        A :class:`FavoriteFilamentResult` describing the favorite, the favorite
        appearance and the synced design material.

    Raises:
        FavoritesError: If the application/design is unusable, the template is
            missing, the favorite cannot be colored, or the design material
            cannot be replaced.
    """
    if app is None:
        raise FavoritesError("ensure_favorite_filament requires the Fusion Application, got None")
    if design is None:
        raise FavoritesError("ensure_favorite_filament requires a Fusion design, got None")
    catalog_id = _require_text(_entry_field(entry, "id"), "catalog entry id")
    normalized_hex = _entry_color_hex(entry)
    display_name = _display_name(entry, "display_name", "catalog entry", catalog_id)
    description = _build_description(entry)

    template = _resolve_template(app, design, entry, template_material)
    favorite = _ensure_favorite_material(
        app, catalog_id, template, display_name=display_name
    )
    _set_description(favorite, description, "favorite material {!r}".format(catalog_id))
    _set_density(favorite, entry, "favorite material {!r}".format(catalog_id))
    _set_solid_values(favorite, entry, description, "favorite material {!r}".format(catalog_id))
    favorite_appearance = _color_favorite_appearance(app, favorite, catalog_id, normalized_hex)

    design_materials = _safe_get(design, "materials", "the active design")
    if design_materials is None:
        raise FavoritesError(
            "The active design exposes no materials collection; cannot sync {!r}".format(
                catalog_id
            )
        )

    # Design materials carry the filament name, so several spools of one filament
    # share one design material; older copies named by catalog id are found too.
    design_name = display_name or catalog_id
    existing_material = _item_by_name(
        design_materials, design_name, "design materials"
    ) or _item_by_name(design_materials, catalog_id, "design materials")
    replaced_existing = False
    relinked_bodies = 0
    if existing_material is not None and _design_material_matches(
        existing_material, favorite, normalized_hex
    ):
        design_material = existing_material
        if _display_name(existing_material, "name", "a design material", "") != design_name:
            _rename(existing_material, design_name, "design material")
        logger.debug(
            "Design material %r already carries %s; reusing it.", design_name, normalized_hex
        )
    elif existing_material is not None:
        design_material, relinked_bodies = _migrate_design_material(
            design, design_materials, existing_material, favorite, design_name
        )
        replaced_existing = True
    else:
        design_material = _copy_favorite_into_design(design_materials, favorite, design_name)

    design_appearance = _safe_get(design_material, "appearance", "the design material appearance")
    if design_appearance is not None:
        _clear_color_texture(design_appearance)
        _apply_appearance_color_properties(design_appearance, normalized_hex)
    _set_description(design_material, description, "design material {!r}".format(catalog_id))
    _set_density(design_material, entry, "design material {!r}".format(catalog_id))
    _set_solid_values(design_material, entry, description, "design material {!r}".format(catalog_id))
    _warn_on_design_material_color(design_material, catalog_id, normalized_hex)
    logger.info(
        "Favorite filament %r ready (color %s, replaced_existing=%s, relinked_bodies=%d).",
        catalog_id,
        normalized_hex,
        replaced_existing,
        relinked_bodies,
    )
    return FavoriteFilamentResult(
        catalog_id=catalog_id,
        display_name=display_name,
        color_hex=normalized_hex,
        favorite_material=favorite,
        favorite_appearance=favorite_appearance,
        design_material=design_material,
        replaced_existing=replaced_existing,
        relinked_bodies=relinked_bodies,
    )


def import_catalog_to_favorites(
    app: Any,
    design: Any,
    entries: Sequence[Any],
    template_resolver: Optional[Callable[[Any], Any]] = None,
) -> List[FavoriteFilamentResult]:
    """Import every catalog entry into Favorites and sync it into the design.

    The import is fail-fast: the first failing entry raises a
    :class:`FavoritesError` naming its catalog id, leaving earlier entries
    imported (the palette reports the whole batch as failed).

    Args:
        app: Fusion ``Application``.
        design: Active Fusion design.
        entries: Catalog entries.
        template_resolver: Optional callable mapping an entry to a template
            material; used before falling back to the importer module.

    Returns:
        One :class:`FavoriteFilamentResult` per imported entry, in input order.

    Raises:
        FavoritesError: If ``entries`` is ``None`` or any entry fails.
    """
    if entries is None:
        raise FavoritesError("entries must be a sequence of catalog entries, got None")
    results: List[FavoriteFilamentResult] = []
    for index, entry in enumerate(entries):
        raw_identifier = _entry_field(entry, "id")
        catalog_id = (
            raw_identifier.strip()
            if isinstance(raw_identifier, str) and raw_identifier.strip()
            else "<entry #{}>".format(index)
        )
        try:
            template = template_resolver(entry) if template_resolver is not None else None
            if template_resolver is not None and template is None:
                logger.debug(
                    "template_resolver returned None for %r; falling back to the importer.",
                    catalog_id,
                )
            results.append(
                ensure_favorite_filament(app, design, entry, template_material=template)
            )
        except FavoritesError as exc:
            raise FavoritesError(
                "Failed to add catalog entry {!r} to Favorites: {}".format(catalog_id, exc)
            ) from exc
        except Exception as exc:
            logger.exception(
                "Unexpected failure importing catalog entry %r to Favorites.", catalog_id
            )
            raise FavoritesError(
                "Failed to add catalog entry {!r} to Favorites: {}".format(catalog_id, exc)
            ) from exc
    logger.info("Imported %d catalog filament(s) into Favorites.", len(results))
    return results


# ---------------------------------------------------------------------------
# Self-test doubles and checks (only used by the __main__ self-test)
# ---------------------------------------------------------------------------


class FakeColor:
    """Test double for ``adsk.core.Color``."""

    def __init__(self, red: int, green: int, blue: int, opacity: int = 255) -> None:
        self.red = red
        self.green = green
        self.blue = blue
        self.opacity = opacity


class FakeAppearance:
    """Test double for a regular, color-editable ``adsk.core.Appearance``."""

    def __init__(
        self, name: str, color: Optional[FakeColor] = None, appearance_id: Optional[str] = None
    ) -> None:
        self.name = name
        self.color = color if color is not None else FakeColor(255, 255, 255, 255)
        self.id = appearance_id if appearance_id is not None else "appearance-" + name


class ProxyAppearance:
    """Test double for the favorite-material appearance color proxy quirk.

    The real Fusion API raises ``RuntimeError("InternalValidationError :
    appearances")`` when assigning ``.color`` on a favorite material's
    appearance, while still applying the color.  This double models exactly
    that behavior.
    """

    def __init__(self, name: str, appearance_id: str, color: FakeColor) -> None:
        self.name = name
        self.id = appearance_id
        self._color = color
        self.set_attempts = 0
        self.applied_attempts = 0

    @property
    def color(self) -> FakeColor:
        """Current (already applied) color."""
        return self._color

    @color.setter
    def color(self, value: FakeColor) -> None:
        self.set_attempts += 1
        self._color = value
        self.applied_attempts += 1
        raise RuntimeError("InternalValidationError : appearances")


class FakeLibraryMaterial:
    """Test double for a MaterialLibrary material used as a favorites source."""

    def __init__(self, name: str, appearance: Optional[FakeAppearance] = None) -> None:
        self.name = name
        self.id = "library-material-" + name
        self.appearance = appearance if appearance is not None else FakeAppearance(name)


class FakeFavoriteMaterial:
    """Test double for a favorite ``Material`` (name/description writable)."""

    def __init__(self, name: str, material_id: str, appearance: ProxyAppearance) -> None:
        self.name = name
        self.id = material_id
        self._appearance = appearance
        self.description: Optional[str] = None

    @property
    def appearance(self) -> ProxyAppearance:
        """Appearance clone linked to this favorite material."""
        return self._appearance


class FakeFavoriteAppearances:
    """Test double for ``app.favoriteAppearances``."""

    def __init__(self) -> None:
        self._appearances: List[FakeAppearance] = []

    @property
    def count(self) -> int:
        """Number of favorite appearances."""
        return len(self._appearances)

    def item(self, index: int) -> Any:
        """Return the appearance at ``index``, or ``None``."""
        if 0 <= index < len(self._appearances):
            return self._appearances[index]
        return None

    def itemByName(self, name: str) -> Optional[FakeAppearance]:
        """Return the first appearance named ``name``, or ``None``."""
        for appearance in self._appearances:
            if appearance.name == name:
                return appearance
        return None

    def itemById(self, identifier: str) -> Optional[FakeAppearance]:
        """Return the appearance with ``identifier``, or ``None``."""
        for appearance in self._appearances:
            if appearance.id == identifier:
                return appearance
        return None

    def add(self, appearance: Any) -> FakeAppearance:
        """Create a favorite-appearance copy of ``appearance``."""
        source_color = getattr(appearance, "color", None)
        color = (
            FakeColor(source_color.red, source_color.green, source_color.blue)
            if source_color is not None
            else FakeColor(255, 255, 255, 255)
        )
        new_id = "favorite-appearance-{}".format(len(self._appearances) + 1)
        copy = FakeAppearance(
            _display_name(appearance, "name", "an appearance", "Favorite Appearance"),
            color,
            new_id,
        )
        self._appearances.append(copy)
        return copy

    def add_linked(self, appearance_id: str, name: str, color: FakeColor) -> FakeAppearance:
        """Register an appearance clone that shares a favorite appearance id."""
        linked = FakeAppearance(
            name, FakeColor(color.red, color.green, color.blue), appearance_id
        )
        self._appearances.append(linked)
        return linked


class FakeFavoriteMaterials:
    """Test double for ``app.favoriteMaterials``."""

    def __init__(self, favorite_appearances: FakeFavoriteAppearances) -> None:
        self._materials: List[FakeFavoriteMaterial] = []
        self._appearances = favorite_appearances
        self.added: List[Any] = []
        self._next_index = 1

    @property
    def count(self) -> int:
        """Number of favorite materials."""
        return len(self._materials)

    def item(self, index: int) -> Any:
        """Return the material at ``index``, or ``None``."""
        if 0 <= index < len(self._materials):
            return self._materials[index]
        return None

    def itemByName(self, name: str) -> Optional[FakeFavoriteMaterial]:
        """Return the first favorite material named ``name``, or ``None``."""
        for material in self._materials:
            if material.name == name:
                return material
        return None

    def add(self, material: Any) -> FakeFavoriteMaterial:
        """Create a favorite material from a library material (like Fusion)."""
        self.added.append(material)
        index = self._next_index
        self._next_index += 1
        source_appearance = getattr(material, "appearance", None)
        source_color = getattr(source_appearance, "color", None)
        color = (
            FakeColor(source_color.red, source_color.green, source_color.blue)
            if source_color is not None
            else FakeColor(255, 255, 255, 255)
        )
        # Like Fusion: a favorite's own appearance id starts with its material id.
        appearance_id = "favorite-material-{}_physmat_aspects:Prism".format(index)
        appearance_name = _display_name(
            source_appearance, "name", "a library material appearance", "Appearance"
        )
        proxy = ProxyAppearance(appearance_name, appearance_id, color)
        favorite = FakeFavoriteMaterial(
            _display_name(material, "name", "a library material", "Material"),
            "favorite-material-{}".format(index),
            proxy,
        )
        self._materials.append(favorite)
        self._appearances.add_linked(appearance_id, appearance_name, color)
        return favorite


class FakeApp:
    """Test double for ``adsk.core.Application`` favorites collections."""

    def __init__(self) -> None:
        self.favoriteAppearances = FakeFavoriteAppearances()
        self.favoriteMaterials = FakeFavoriteMaterials(self.favoriteAppearances)


class FakeDesignMaterial:
    """Test double for a design material (appearance read-only, deleteMe)."""

    def __init__(self, name: str, material_id: str, appearance: FakeAppearance) -> None:
        self.name = name
        self.id = material_id
        self._appearance = appearance
        self.description: Optional[str] = None
        self.deleted = False
        self.delete_guard: Optional[Callable[[], bool]] = None
        self._owner: Optional["FakeDesignMaterials"] = None

    @property
    def appearance(self) -> FakeAppearance:
        """Read-only material appearance."""
        return self._appearance

    def deleteMe(self) -> None:
        """Delete the material, refusing when a test double reports it is in use."""
        if self.delete_guard is not None and self.delete_guard():
            raise RuntimeError("simulated Fusion failure: material is still in use")
        self.deleted = True
        if self._owner is not None:
            self._owner.remove(self)


class FakeDesignMaterials:
    """Test double for ``adsk.core.Materials`` in the design."""

    def __init__(self, materials: Optional[Sequence[FakeDesignMaterial]] = None) -> None:
        self._materials = list(materials or [])
        for material in self._materials:
            material._owner = self

    @property
    def count(self) -> int:
        """Number of design materials."""
        return len(self._materials)

    def item(self, index: int) -> Any:
        """Return the material at ``index``, or ``None``."""
        if 0 <= index < len(self._materials):
            return self._materials[index]
        return None

    def itemByName(self, name: str) -> Optional[FakeDesignMaterial]:
        """Return the first design material named ``name``, or ``None``."""
        for material in self._materials:
            if material.name == name:
                return material
        return None

    def addByCopy(self, source: Any, name: str) -> FakeDesignMaterial:
        """Copy a favorite material into the design with its current color."""
        if self.itemByName(name) is not None:
            raise RuntimeError("duplicate design material name: {}".format(name))
        source_appearance = getattr(source, "appearance", None)
        source_color = getattr(source_appearance, "color", None)
        color = (
            FakeColor(source_color.red, source_color.green, source_color.blue)
            if source_color is not None
            else FakeColor(255, 255, 255, 255)
        )
        material = FakeDesignMaterial(
            name,
            "design-material-{}".format(len(self._materials) + 1),
            # Like Fusion, the copy keeps the source appearance (template) name.
            FakeAppearance(
                _display_name(source_appearance, "name", "a source appearance", name + " Appearance"),
                color,
            ),
        )
        material._owner = self
        self._materials.append(material)
        return material

    def add(self, material: FakeDesignMaterial) -> FakeDesignMaterial:
        """Register an already created design material."""
        material._owner = self
        self._materials.append(material)
        return material

    def remove(self, material: FakeDesignMaterial) -> None:
        """Remove ``material`` from the collection when present."""
        if material in self._materials:
            self._materials.remove(material)


class FakeBody:
    """Test double for ``adsk.fusion.BRepBody``."""

    def __init__(self, name: str, material: Optional[FakeDesignMaterial] = None) -> None:
        self.name = name
        self.material = material


class FakeCollection:
    """Test double for a Fusion ``count``/``item(index)`` collection."""

    def __init__(self, items: Optional[Sequence[Any]] = None) -> None:
        self._items = list(items or [])

    @property
    def count(self) -> int:
        """Number of items."""
        return len(self._items)

    def item(self, index: int) -> Any:
        """Return the item at ``index``, or ``None``."""
        if 0 <= index < len(self._items):
            return self._items[index]
        return None


class FakeComponent:
    """Test double for ``adsk.fusion.Component``."""

    def __init__(
        self,
        name: str,
        bodies: Optional[Sequence[FakeBody]] = None,
        occurrences: Optional[Sequence["FakeOccurrence"]] = None,
    ) -> None:
        self.name = name
        self.bRepBodies = FakeCollection(bodies)
        self.meshBodies = FakeCollection([])
        self.occurrences = FakeCollection(occurrences)


class FakeOccurrence:
    """Test double for ``adsk.fusion.Occurrence``."""

    def __init__(self, name: str, component: FakeComponent) -> None:
        self.name = name
        self.component = component


class FakeDesign:
    """Test double for ``adsk.fusion.Design``."""

    def __init__(
        self,
        root_component: Optional[FakeComponent] = None,
        materials: Optional[FakeDesignMaterials] = None,
    ) -> None:
        self.rootComponent = root_component
        self.materials = materials if materials is not None else FakeDesignMaterials()


class FakeCatalogEntry:
    """Test double for a catalog entry used by the favorites workflow."""

    def __init__(
        self,
        entry_id: str,
        color_hex: str = "#161616",
        template_material: Optional[str] = "PLA Plastic",
        category: str = "PLA",
        display_name: Optional[str] = None,
        manufacturer: str = "Bambu Lab",
        product_line: str = "PLA Basic",
        color_name: str = "Black",
        slicer_filament_type: str = "PLA",
        density_g_cm3: float = 1.24,
    ) -> None:
        self.id = entry_id
        self.color_hex = color_hex
        self.template_material = template_material
        self.category = category
        self.display_name = display_name or (
            manufacturer + " - " + product_line + " - " + color_name
        )
        self.manufacturer = manufacturer
        self.product_line = product_line
        self.color_name = color_name
        self.slicer_filament_type = slicer_filament_type
        self.density_g_cm3 = density_g_cm3


def _expect(condition: bool, message: str) -> None:
    """Raise AssertionError with ``message`` when ``condition`` is false."""
    if not condition:
        raise AssertionError(message)


def _install_fake_importer(
    template_material: Any = None,
    error: Optional[BaseException] = None,
    calls: Optional[List[Dict[str, Any]]] = None,
    missing: Sequence[str] = (),
    densities: Optional[List[Tuple[Any, float]]] = None,
) -> Callable[[], None]:
    """Install a fake ``importer`` module into ``sys.modules`` and return a restore callback."""

    def find_template_material(design: Any, template_name: str, app: Any = None) -> Any:
        if calls is not None:
            calls.append({"design": design, "template_name": template_name, "app": app})
        if error is not None:
            raise error
        if template_name in missing:
            raise LookupError("simulated missing template {!r}".format(template_name))
        return template_material

    def set_material_density(material: Any, density_g_cm3: float) -> bool:
        if densities is not None:
            densities.append((material, density_g_cm3))
        return True

    module = types.ModuleType("importer")
    module.find_template_material = find_template_material  # type: ignore[attr-defined]
    module.set_material_density = set_material_density  # type: ignore[attr-defined]
    previous = testing.sibling_get(__package__, "importer")
    testing.sibling_set(__package__, "importer", module)

    def restore() -> None:
        if previous is None:
            testing.sibling_pop(__package__, "importer")
        else:
            testing.sibling_set(__package__, "importer", previous)

    return restore


def _make_template_material() -> FakeLibraryMaterial:
    """Return the fake library template material used by the checks."""
    return FakeLibraryMaterial(
        "PLA Plastic",
        FakeAppearance("PLA Plastic Appearance", FakeColor(128, 128, 128, 255)),
    )


def _make_entry(
    entry_id: str = "bambu_pla_basic_black", color_hex: str = "#161616"
) -> FakeCatalogEntry:
    """Return the fake catalog entry used by the checks."""
    return FakeCatalogEntry(entry_id, color_hex=color_hex)


def _design_material_hex(design_material: FakeDesignMaterial) -> str:
    """Return the hex color of a fake design material."""
    return color_hex(design_material.appearance.color)


def _check_color_hex() -> None:
    """color_hex converts Fusion colors and validates unusable values."""
    _expect(
        color_hex(FakeColor(0x16, 0x16, 0x16, 255)) == "#161616",
        "color_hex must return uppercase #RRGGBB",
    )
    _expect(
        color_hex(types.SimpleNamespace(red=255, green=255, blue=255)) == "#FFFFFF",
        "color_hex must tolerate a color without opacity",
    )
    invalid = 0
    for bad_color in (
        None,
        types.SimpleNamespace(red=1, green=2),
        "not-a-color",
        FakeColor(True, 2, 3),
    ):
        try:
            color_hex(bad_color)
        except FavoritesError:
            invalid += 1
    _expect(invalid == 4, "invalid colors must raise FavoritesError")


def _check_summary_and_lookup() -> None:
    """favorites_summary and find_favorite reflect the Favorites collections."""
    app = FakeApp()
    _expect(
        favorites_summary(app) == {"count": 0, "names": []},
        "an empty Favorites library must report zero materials",
    )
    _expect(find_favorite(app, "missing") is None, "find_favorite must return None when absent")

    template = _make_template_material()
    favorite = app.favoriteMaterials.add(template)
    favorite.name = "bambu_pla_basic_black"
    summary = favorites_summary(app)
    _expect(summary["count"] == 1, "summary count must include the added favorite")
    _expect(summary["names"] == ["bambu_pla_basic_black"], "summary names must list favorites")
    _expect(
        find_favorite(app, "bambu_pla_basic_black") is favorite,
        "find_favorite must return the favorite material",
    )
    _expect(find_favorite(app, "other") is None, "find_favorite must not match other names")


def _check_clean_import() -> None:
    """ensure_favorite_filament creates a colored favorite and design material."""
    template = _make_template_material()
    calls: List[Dict[str, Any]] = []
    restore = _install_fake_importer(template, calls=calls)
    try:
        app = FakeApp()
        design = FakeDesign(root_component=FakeComponent("Root"))
        entry = _make_entry()
        result = ensure_favorite_filament(app, design, entry)
    finally:
        restore()

    _expect(result.catalog_id == entry.id, "result catalog id mismatch")
    _expect(result.display_name == entry.display_name, "result display name mismatch")
    _expect(result.color_hex == "#161616", "result color must be normalized")
    _expect(result.replaced_existing is False, "a fresh import must not replace anything")
    _expect(result.relinked_bodies == 0, "a fresh import must not relink bodies")

    favorite = result.favorite_material
    _expect(
        favorite.name == entry.display_name,
        "favorite material must use the Brand - Material - Color name, got {!r}".format(
            favorite.name
        ),
    )
    _expect(app.favoriteMaterials.count == 1, "exactly one favorite material must exist")
    _expect(app.favoriteAppearances.count == 1, "exactly one favorite appearance must exist")
    _expect(
        result.favorite_appearance is app.favoriteAppearances.itemById(favorite.appearance.id),
        "the favorite appearance must be located by appearance id",
    )
    _expect(
        favorite.appearance.set_attempts == 1 and favorite.appearance.applied_attempts == 1,
        "the verified proxy color assignment must have been attempted and ignored",
    )
    _expect(
        color_hex(result.favorite_appearance.color) == "#161616",
        "the FavoriteAppearances item must carry the catalog color",
    )
    expected_description = "Filamatch | PLA | Bambu Lab - PLA Basic - Black"
    _expect(favorite.description == expected_description, "favorite description mismatch")
    _expect(
        result.design_material.description == expected_description,
        "design material description mismatch",
    )
    _expect(
        result.design_material.name == entry.display_name,
        "the design material must be named after the filament",
    )
    _expect(
        _design_material_hex(result.design_material) == "#161616",
        "the copied design material must carry the corrected color",
    )
    _expect(design.materials.count == 1, "the design must contain exactly one material")
    _expect(len(calls) == 1, "the importer must resolve the template exactly once")
    _expect(
        calls[0]["template_name"] == "Plastic, Opaque Black",
        "the colour-matched swatch template must be requested first",
    )
    _expect(calls[0]["app"] is app, "the application must be forwarded to the importer")


def _check_favorite_reuse() -> None:
    """Repeated imports reuse the favorite and the design material (idempotent)."""
    template = _make_template_material()
    app = FakeApp()
    design = FakeDesign(root_component=FakeComponent("Root"))
    entry = _make_entry()

    first = ensure_favorite_filament(app, design, entry, template_material=template)
    _expect(
        app.favoriteMaterials.added == [template],
        "the template material must be added to Favorites once",
    )
    second = ensure_favorite_filament(app, design, entry, template_material=template)

    _expect(
        second.favorite_material is first.favorite_material,
        "repeated imports must reuse the favorite material",
    )
    _expect(
        second.design_material is first.design_material,
        "repeated imports must reuse the design material",
    )
    _expect(second.replaced_existing is False, "reuse must not report a replacement")
    _expect(app.favoriteMaterials.count == 1, "reuse must not duplicate favorites")
    _expect(app.favoriteAppearances.count == 1, "reuse must not duplicate favorite appearances")
    _expect(design.materials.count == 1, "reuse must not duplicate design materials")
    _expect(
        len(app.favoriteMaterials.added) == 1,
        "the template material must only be added once",
    )


def _check_migration() -> None:
    """An existing gray design material is replaced and its bodies relinked."""
    template = _make_template_material()
    entry = _make_entry()
    gray = FakeDesignMaterial(
        entry.id, "design-gray", FakeAppearance("Gray", FakeColor(0x80, 0x80, 0x80))
    )
    placeholder = FakeDesignMaterial(
        "PLA Plastic",
        "design-placeholder",
        FakeAppearance("Placeholder", FakeColor(0xEE, 0xEE, 0xEE)),
    )
    design = FakeDesign(
        root_component=FakeComponent("Root"),
        materials=FakeDesignMaterials([gray, placeholder]),
    )
    bodies = [
        FakeBody("Bracket", gray),
        FakeBody("Bushing", gray),
        FakeBody("Clamp Jaw", gray),
    ]
    nested = FakeComponent("Sub Assembly", bodies=[bodies[2]])
    design.rootComponent = FakeComponent(
        "Root",
        bodies=bodies[:2],
        occurrences=[FakeOccurrence("Component:1", nested)],
    )
    gray.delete_guard = lambda: any(body.material is gray for body in bodies)

    restore = _install_fake_importer(template)
    try:
        apple = FakeApp()
        result = ensure_favorite_filament(apple, design, entry)
    finally:
        restore()

    _expect(result.replaced_existing is True, "migration must report a replacement")
    _expect(result.relinked_bodies == 3, "migration must relink all three bodies")
    _expect(gray.deleted is True, "the old gray design material must be deleted")
    _expect(
        design.materials.itemByName(entry.display_name) is result.design_material,
        "the new design material must be registered under the filament name",
    )
    _expect(
        _design_material_hex(result.design_material) == "#161616",
        "the replacement design material must carry the catalog color",
    )
    for body in bodies:
        _expect(
            body.material is result.design_material,
            "body {!r} must be relinked to the new design material".format(body.name),
        )
    _expect(
        design.materials.itemByName("PLA Plastic") is placeholder,
        "the placeholder material must be preserved",
    )
    _expect(design.materials.count == 2, "design must keep placeholder + replacement material")


def _check_migration_without_placeholder() -> None:
    """Without another design material the copy-relink-delete migration still works."""
    template = _make_template_material()
    entry = _make_entry()
    gray = FakeDesignMaterial(
        entry.id, "design-gray", FakeAppearance("Gray", FakeColor(0x80, 0x80, 0x80))
    )
    design = FakeDesign(
        root_component=FakeComponent("Root"),
        materials=FakeDesignMaterials([gray]),
    )
    body = FakeBody("Only Body", gray)
    design.rootComponent = FakeComponent("Root", bodies=[body])
    gray.delete_guard = lambda: body.material is gray

    restore = _install_fake_importer(template)
    try:
        app = FakeApp()
        result = ensure_favorite_filament(app, design, entry)
    finally:
        restore()

    _expect(
        result.replaced_existing is True,
        "placeholder-free migration must replace the material",
    )
    _expect(result.relinked_bodies == 1, "placeholder-free migration must relink one body")
    _expect(gray.deleted is True, "the old material must be deleted")
    _expect(body.material is result.design_material, "the body must be relinked after the copy")
    _expect(design.materials.count == 1, "only the replacement material must remain")


def _check_missing_template() -> None:
    """Missing templates and a missing importer raise actionable FavoritesError."""
    app = FakeApp()
    design = FakeDesign(root_component=FakeComponent("Root"))
    entry = _make_entry()

    restore = _install_fake_importer(error=RuntimeError("simulated missing template"))
    try:
        try:
            ensure_favorite_filament(app, design, entry)
        except FavoritesError as exc:
            message = str(exc)
            _expect(
                "PLA Plastic" in message,
                "the error must name the template: {}".format(message),
            )
            _expect(
                "catalog/filaments.json" in message,
                "the error must point at the catalog: {}".format(message),
            )
            _expect(
                "Material Library" in message,
                "the error must mention loading the Material Library: {}".format(message),
            )
        else:
            raise AssertionError("a missing template must raise FavoritesError")
    finally:
        restore()

    previous = testing.sibling_get(__package__, "importer")
    testing.sibling_set(__package__, "importer", None)
    try:
        try:
            ensure_favorite_filament(app, design, entry)
        except FavoritesError as exc:
            _expect(
                "importer" in str(exc).lower(),
                "the error must mention the unavailable importer: {}".format(exc),
            )
        else:
            raise AssertionError("a missing importer module must raise FavoritesError")
    finally:
        if previous is None:
            testing.sibling_pop(__package__, "importer")
        else:
            testing.sibling_set(__package__, "importer", previous)


def _check_import_batch() -> None:
    """import_catalog_to_favorites imports batches and fails fast with the entry id."""
    template = _make_template_material()
    app = FakeApp()
    design = FakeDesign(root_component=FakeComponent("Root"))
    entries = [
        _make_entry("bambu_pla_basic_black", "#161616"),
        FakeCatalogEntry(
            "bambu_petg_hf_white",
            color_hex="#FFFFFF",
            display_name="Bambu Lab - PETG HF - White",
            product_line="PETG HF",
            color_name="White",
            slicer_filament_type="PETG",
            density_g_cm3=1.27,
        ),
    ]

    def resolver(entry: Any) -> Any:
        return template

    results = import_catalog_to_favorites(app, design, entries, template_resolver=resolver)
    _expect([result.catalog_id for result in results] == [entry.id for entry in entries],
            "batch import must preserve entry order")
    _expect(app.favoriteMaterials.count == 2, "batch import must create both favorites")
    _expect(app.favoriteAppearances.count == 2, "batch import must create both appearances")
    _expect(design.materials.count == 2, "batch import must sync both design materials")
    _expect(
        _design_material_hex(design.materials.itemByName("Bambu Lab - PETG HF - White"))
        == "#FFFFFF",
        "batch import must color every design material",
    )

    def failing_resolver(entry: Any) -> Any:
        if entry.id == "broken_entry":
            raise RuntimeError("simulated resolver failure")
        return template

    failing_app = FakeApp()
    failing_design = FakeDesign(root_component=FakeComponent("Root"))
    failing_entries = [
        _make_entry("good_entry", "#010203"),
        _make_entry("broken_entry", "#040506"),
    ]
    try:
        import_catalog_to_favorites(
            failing_app, failing_design, failing_entries, template_resolver=failing_resolver
        )
    except FavoritesError as exc:
        _expect(
            "broken_entry" in str(exc),
            "the fail-fast error must name the failing catalog id: {}".format(exc),
        )
    else:
        raise AssertionError("a failing entry must raise FavoritesError")
    _expect(
        failing_design.materials.count == 1,
        "entries imported before the failure must remain in the design",
    )


def _check_swatch_templates() -> None:
    """Favorites copy the nearest-coloured stock template and get the catalog density."""
    expected = {
        "#489FDF": "Laminate, Blue, Matte",
        "#00AE42": "FR4",
        "#DACF9D": "PAEK Plastic",
        "#000000": "Plastic, Opaque Black",
        "#C6C6C6": "PC/ABS Plastic",
        "#FFFFFF": "ABS Plastic",
        "#C12E1F": "Laminate, Red, Matte",
        "#F7D959": "Phenolic Resin",
        "#7D6556": "PEEK",
    }
    for value, name in expected.items():
        _expect(
            nearest_swatch_template(value) == name,
            "{} must pick {!r}, got {!r}".format(value, name, nearest_swatch_template(value)),
        )

    def requested(entry: FakeCatalogEntry, missing: Sequence[str] = ()) -> List[str]:
        calls: List[Dict[str, Any]] = []
        densities: List[Tuple[Any, float]] = []
        restore = _install_fake_importer(
            _make_template_material(), calls=calls, missing=missing, densities=densities
        )
        try:
            result = ensure_favorite_filament(
                FakeApp(), FakeDesign(root_component=FakeComponent("Root")), entry
            )
        finally:
            restore()
        _expect(
            [density for _material, density in densities]
            == ([entry.density_g_cm3] * 2 if entry.density_g_cm3 else []),
            "the catalog density must be written onto the favorite and design material",
        )
        _expect(
            densities[0][0] is result.favorite_material if densities else True,
            "the density must be written onto the favorite first",
        )
        return [call["template_name"] for call in calls]

    azure = _make_entry("bambu_abs_azure", "#489FDF")
    _expect(
        requested(azure) == ["Laminate, Blue, Matte"],
        "a coloured filament must use the nearest swatch template",
    )
    _expect(
        requested(azure, missing=("Laminate, Blue, Matte",))
        == ["Laminate, Blue, Matte", "PLA Plastic"],
        "a missing swatch template must fall back to the catalog template",
    )
    clear = FakeCatalogEntry("bambu_petg_clear", color_hex="#489FDF", color_name="Clear Blue")
    _expect(requested(clear) == ["PLA Plastic"], "clear filaments keep the catalog template")
    no_density = FakeCatalogEntry("no_density", color_hex="#489FDF", density_g_cm3=0)
    _expect(
        requested(no_density) == ["PLA Plastic"],
        "without a catalog density the catalog template (and its density) is kept",
    )
    same = FakeCatalogEntry("white_abs", color_hex="#FFFFFF", template_material="ABS Plastic")
    _expect(
        requested(same) == ["ABS Plastic"],
        "a catalog template that already matches is requested once",
    )


def _check_stale_swatch_favorites() -> None:
    """Favorites made from a template that does not match their colour are listed."""

    def favorite(
        name: str, appearance: str, rgb: Tuple[int, int, int], ours: bool = True, own: bool = True
    ) -> Any:
        material_id = "id-" + name
        return types.SimpleNamespace(
            name=name,
            id=material_id,
            description=(MATERIAL_DESCRIPTION_PREFIX + "ABS | x") if ours else "Autodesk",
            appearance=types.SimpleNamespace(
                name=appearance,
                color=FakeColor(*rgb, 255),
                id=(material_id if own else "PrismMaterial-018") + "_physmat_aspects:Prism",
            ),
        )

    items = [
        favorite("Bambu Lab - ABS - Azure", "ABS (White)", (0x48, 0x9F, 0xDF)),
        favorite("Bambu Lab - ABS - Bambu Green", "FR4", (0x00, 0xAE, 0x42)),
        favorite("Bambu Lab - ABS - White", "ABS (White)", (0xFF, 0xFF, 0xFF)),
        favorite("Bambu Lab - PETG - Clear", "Polycarbonate (Clear)", (0xFF, 0xFF, 0xFF)),
        favorite("Somebody's Steel", "ABS (White)", (0x10, 0x10, 0x10), ours=False),
        favorite("Bambu Lab - ABS - Beige", "Steel - Satin", (0x9F, 0x9F, 0x9F), own=False),
        favorite("Bambu Lab - ABS - Red", "Plastic - Matte (Gray)", (0x9F, 0x9F, 0x9F)),
    ]
    catalog = [
        FakeCatalogEntry("red", color_hex="#C12E1F", display_name="Bambu Lab - ABS - Red"),
        FakeCatalogEntry("beige", color_hex="#DACF9D", display_name="Bambu Lab - ABS - Beige"),
    ]
    app = types.SimpleNamespace(
        favoriteMaterials=types.SimpleNamespace(count=len(items), item=items.__getitem__)
    )
    appearances = {
        "Laminate, Blue, Matte": "Plastic - Matte (Blue)",
        "Laminate, Red, Matte": "Plastic - Matte (Red)",
        "PC/ABS Plastic": "Plastic - Matte (Gray)",
        "PAEK Plastic": "Paek (Beige)",
        "FR4": "FR4",
        "ABS Plastic": "ABS (White)",
    }
    module = types.ModuleType("importer")
    module.find_template_material = lambda design, name, app=None: types.SimpleNamespace(  # type: ignore[attr-defined]
        appearance=types.SimpleNamespace(name=appearances[name])
    )
    module.get_material_density = lambda material: None  # type: ignore[attr-defined]
    previous = testing.sibling_get(__package__, "importer")
    testing.sibling_set(__package__, "importer", module)
    try:
        stale = stale_swatch_favorites(app)
        with_catalog = stale_swatch_favorites(app, catalog)
    finally:
        if previous is None:
            testing.sibling_pop(__package__, "importer")
        else:
            testing.sibling_set(__package__, "importer", previous)
    _expect(
        stale == ["Bambu Lab - ABS - Azure"],
        "only our coloured favorite with the white template must be listed, got {}".format(stale),
    )
    _expect(
        with_catalog == ["Bambu Lab - ABS - Azure", "Bambu Lab - ABS - Red"],
        "the catalog colour must win and an unreadable favorite must be skipped, got {}".format(
            with_catalog
        ),
    )


def _check_design_template_migration() -> None:
    """A right-coloured design copy with the old template is replaced; twins are untouched."""
    template = FakeLibraryMaterial(
        "Plastic, Opaque Black", FakeAppearance("Plastic - Glossy (Black)", FakeColor(0x19, 0x19, 0x19))
    )
    entry = _make_entry()
    old = FakeDesignMaterial(
        entry.display_name, "shared-id", FakeAppearance("ABS (White)", FakeColor(0x16, 0x16, 0x16))
    )
    twin = FakeDesignMaterial(
        "bambuddy_spool_2", "shared-id", FakeAppearance("ABS (White)", FakeColor(0x16, 0x16, 0x16))
    )
    plate = FakeBody("Plate", old)
    other = FakeBody("Other", twin)
    design = FakeDesign(
        root_component=FakeComponent("Root", bodies=[plate, other]),
        materials=FakeDesignMaterials([old, twin]),
    )
    old.delete_guard = lambda: plate.material is old
    restore = _install_fake_importer(template)
    try:
        result = ensure_favorite_filament(FakeApp(), design, entry)
    finally:
        restore()
    _expect(result.replaced_existing and old.deleted, "the white-template copy must be replaced")
    _expect(
        plate.material is result.design_material
        and result.design_material.appearance.name == "Plastic - Glossy (Black)"
        and result.design_material.name == entry.display_name,
        "the body must be relinked to a copy with the new template",
    )
    _expect(
        other.material is twin and not twin.deleted,
        "a material that only shares the id must keep its bodies",
    )


def _check_old_favorite_replacement() -> None:
    """Old favorites with another template are renamed OLD - and replaced once."""
    template = FakeLibraryMaterial(
        "Laminate, Blue, Matte", FakeAppearance("Plastic - Matte (Blue)")
    )
    entry = FakeCatalogEntry(
        "bambu_abs_azure", color_hex="#489FDF", product_line="ABS", color_name="Azure"
    )
    name = entry.display_name

    def old_favorite(app: FakeApp, ours: bool = True) -> FakeFavoriteMaterial:
        material = app.favoriteMaterials.add(FakeLibraryMaterial(name, FakeAppearance("ABS (White)")))
        material.description = (MATERIAL_DESCRIPTION_PREFIX + "ABS | x") if ours else None
        return material

    def import_once(app: FakeApp) -> FavoriteFilamentResult:
        restore = _install_fake_importer(template)
        try:
            return ensure_favorite_filament(
                app, FakeDesign(root_component=FakeComponent("Root")), entry
            )
        finally:
            restore()

    app = FakeApp()
    first_old = old_favorite(app)
    second_old = old_favorite(app)
    result = import_once(app)
    _expect(
        first_old.name == second_old.name == OLD_FAVORITE_PREFIX + name,
        "every old copy must be renamed OLD - <name>, got {!r}/{!r}".format(
            first_old.name, second_old.name
        ),
    )
    _expect(
        result.favorite_material.name == name
        and result.favorite_material.appearance.name == "Plastic - Matte (Blue)",
        "a new favorite with the colour template must take the name",
    )
    _expect(app.favoriteMaterials.count == 3, "exactly one new favorite must be added")
    again = import_once(app)
    _expect(
        again.favorite_material is result.favorite_material and app.favoriteMaterials.count == 3,
        "a later import must reuse the new favorite",
    )

    user_app = FakeApp()
    user_owned = old_favorite(user_app, ours=False)
    user_result = import_once(user_app)
    _expect(
        user_result.favorite_material is user_owned and user_owned.name == name,
        "a favorite the user made must be reused, never renamed",
    )

    unread_app = FakeApp()
    unread = old_favorite(unread_app)
    unread.appearance.id = "PrismMaterial-018_physmat_aspects:Prism-256"
    unread_result = import_once(unread_app)
    _expect(
        unread_result.favorite_material is unread and unread.name == name,
        "a favorite whose appearance cannot be read must be reused, never renamed",
    )


def _check_replace_stale_favorites() -> None:
    """Every stale favorite is replaced in Favorites only, keeping colour and density."""
    app = FakeApp()

    def favorite(name: str, rgb: Tuple[int, int, int], appearance: str = "ABS (White)") -> Any:
        material = app.favoriteMaterials.add(
            FakeLibraryMaterial(name, FakeAppearance(appearance, FakeColor(*rgb, 255)))
        )
        material.description = MATERIAL_DESCRIPTION_PREFIX + "ABS | " + name
        return material

    azure = favorite("Bambu Lab - ABS - Azure", (0x48, 0x9F, 0xDF))
    beige = favorite("Bambu Lab - ABS - Beige", (0xDA, 0xCF, 0x9D))
    white = favorite("Bambu Lab - ABS - White", (0xFF, 0xFF, 0xFF))
    appearances = {
        "Laminate, Blue, Matte": "Plastic - Matte (Blue)",
        "PAEK Plastic": "Paek (Beige)",
        "ABS Plastic": "ABS (White)",
    }
    densities: List[Tuple[Any, float]] = []
    module = types.ModuleType("importer")
    module.find_template_material = lambda design, name, app=None: FakeLibraryMaterial(  # type: ignore[attr-defined]
        name, FakeAppearance(appearances[name])
    )
    module.get_material_density = lambda material: 1.05  # type: ignore[attr-defined]
    module.set_material_density = lambda material, value: densities.append((material, value)) or True  # type: ignore[attr-defined]
    previous = testing.sibling_get(__package__, "importer")
    testing.sibling_set(__package__, "importer", module)
    try:
        replaced, still_stale = replace_and_list_stale(app)
        again = replace_stale_favorites(app)
    finally:
        if previous is None:
            testing.sibling_pop(__package__, "importer")
        else:
            testing.sibling_set(__package__, "importer", previous)
    _expect(
        replaced == ["Bambu Lab - ABS - Azure", "Bambu Lab - ABS - Beige"],
        "only the stale favorites must be replaced, got {}".format(replaced),
    )
    _expect(again == [], "a second run must find nothing left to replace")
    _expect(
        still_stale == [OLD_FAVORITE_PREFIX + name for name in replaced],
        "the still-stale list must name the renamed old favorites, got {}".format(still_stale),
    )
    _expect(
        azure.name == OLD_FAVORITE_PREFIX + "Bambu Lab - ABS - Azure"
        and beige.name == OLD_FAVORITE_PREFIX + "Bambu Lab - ABS - Beige"
        and white.name == "Bambu Lab - ABS - White",
        "old favorites must be renamed and the matching one kept",
    )
    new_azure = app.favoriteMaterials.itemByName("Bambu Lab - ABS - Azure")
    _expect(
        new_azure is not azure
        and new_azure.appearance.name == "Plastic - Matte (Blue)"
        and color_hex(new_azure.appearance.color) == "#489FDF"
        and new_azure.description == azure.description,
        "the new favorite must use the blue template with the old colour and description",
    )
    _expect(
        [value for _material, value in densities] == [1.05, 1.05],
        "the old density must be carried over",
    )

    grey = favorite("Voxel PLA - PETG - Aurora Green", (0x9F, 0x9F, 0x9F), "Plastic - Matte (Gray)")
    appearances["PC/ABS Plastic"] = "Plastic - Matte (Gray)"
    catalog = [
        FakeCatalogEntry(
            "aurora",
            color_hex="#6D5980",
            display_name="Voxel PLA - PETG - Aurora Green",
            density_g_cm3=1.27,
        )
    ]
    densities.clear()
    testing.sibling_set(__package__, "importer", module)
    try:
        repaired = replace_stale_favorites(app, catalog)
    finally:
        if previous is None:
            testing.sibling_pop(__package__, "importer")
        else:
            testing.sibling_set(__package__, "importer", previous)
    _expect(repaired == [], "a favorite with the right template must not be replaced")
    _expect(
        grey.name == "Voxel PLA - PETG - Aurora Green"
        and color_hex(grey.appearance.color) == "#6D5980"
        and [value for _material, value in densities] == [1.27],
        "a drifted colour and density must be repaired in place",
    )


def _check_foreign_appearance() -> None:
    """After a restart only the favorite's own appearance item is coloured."""
    template = _make_template_material()
    app = FakeApp()
    design = FakeDesign(root_component=FakeComponent("Root"))
    entry = _make_entry()
    first = ensure_favorite_filament(app, design, entry, template_material=template)
    favorite = first.favorite_material
    attempts = favorite.appearance.set_attempts
    again = ensure_favorite_filament(app, design, entry, template_material=template)
    _expect(
        favorite.appearance.set_attempts == attempts,
        "an appearance that already has the colour must not be written again",
    )
    _expect(again.favorite_appearance is first.favorite_appearance, "the same item must be used")

    foreign = ProxyAppearance("Steel - Satin", "PrismMaterial-018_physmat_aspects:Prism", FakeColor(9, 9, 9))
    favorite._appearance = foreign
    recolored = ensure_favorite_filament(
        app, design, _make_entry(color_hex="#123456"), template_material=template
    )
    _expect(foreign.set_attempts == 0, "a foreign appearance must never be recoloured")
    _expect(
        recolored.favorite_appearance is first.favorite_appearance
        and color_hex(recolored.favorite_appearance.color) == "#123456",
        "the own item must be found by the material id prefix and recoloured",
    )

    lost = FakeApp()
    orphan = lost.favoriteMaterials.add(template)
    orphan._appearance = foreign
    lost.favoriteAppearances._appearances.clear()
    try:
        _color_favorite_appearance(lost, orphan, "orphan", "#ABCDEF")
    except FavoritesError:
        pass
    else:
        raise AssertionError("a favorite without an own appearance must fail safely")
    _expect(foreign.set_attempts == 0, "failing safely must not touch the foreign appearance")


class _FailingFavoriteMaterials(FakeFavoriteMaterials):
    """FakeFavoriteMaterials whose ``add`` fails."""

    def add(self, material: Any) -> FakeFavoriteMaterial:
        raise RuntimeError("simulated Fusion failure adding a favorite")


def _check_failed_add_keeps_names() -> None:
    """Old favorites are only renamed OLD - after the new favorite was added."""
    entry = FakeCatalogEntry("bambu_abs_azure", color_hex="#489FDF", product_line="ABS", color_name="Azure")
    app = FakeApp()
    old = app.favoriteMaterials.add(FakeLibraryMaterial(entry.display_name, FakeAppearance("ABS (White)")))
    old.description = MATERIAL_DESCRIPTION_PREFIX + "ABS | x"
    failing = _FailingFavoriteMaterials(app.favoriteAppearances)
    failing._materials = list(app.favoriteMaterials._materials)
    app.favoriteMaterials = failing
    template = FakeLibraryMaterial("Laminate, Blue, Matte", FakeAppearance("Plastic - Matte (Blue)"))
    try:
        ensure_favorite_filament(
            app, FakeDesign(root_component=FakeComponent("Root")), entry, template_material=template
        )
    except FavoritesError:
        pass
    else:
        raise AssertionError("a failed favorite add must raise FavoritesError")
    _expect(old.name == entry.display_name, "a failed add must not rename the old favorite")


class _StubbornBody(FakeBody):
    """FakeBody that refuses to take one material."""

    def __init__(self, name: str, material: Any, refuse: Callable[[Any], bool]) -> None:
        object.__setattr__(self, "_refuse", refuse)
        super().__init__(name, material)

    def __setattr__(self, name: str, value: Any) -> None:
        if name == "material" and self._refuse(value):
            raise RuntimeError("simulated Fusion failure assigning a material")
        object.__setattr__(self, name, value)


def _check_migration_rollback() -> None:
    """A failed relink or delete moves the bodies back and removes the copy."""
    template = _make_template_material()
    for failure in ("relink", "delete"):
        entry = _make_entry()
        gray = FakeDesignMaterial(
            entry.display_name, "design-gray", FakeAppearance("Gray", FakeColor(0x80, 0x80, 0x80))
        )
        materials = FakeDesignMaterials([gray])
        first = FakeBody("First", gray)
        second = _StubbornBody(
            "Second", gray, lambda value: failure == "relink" and value is not gray
        )
        design = FakeDesign(
            root_component=FakeComponent("Root", bodies=[first, second]), materials=materials
        )
        if failure == "delete":
            gray.delete_guard = lambda: True
        try:
            ensure_favorite_filament(FakeApp(), design, entry, template_material=template)
        except FavoritesError:
            pass
        else:
            raise AssertionError("a failed {} must raise FavoritesError".format(failure))
        _expect(
            first.material is gray and second.material is gray and not gray.deleted,
            "after a failed {} every body must be back on the old material".format(failure),
        )
        _expect(
            materials.count == 1 and materials.item(0) is gray,
            "after a failed {} the temporary copy must be removed".format(failure),
        )


class _FakeMechanicalProperty:
    def __init__(self, value: Any, units: str) -> None:
        self.value = value
        self.units = units


class _FakeMechanicalProperties:
    def __init__(self, props: Dict[str, _FakeMechanicalProperty]) -> None:
        self._props = props

    def itemById(self, prop_id: str) -> Optional[_FakeMechanicalProperty]:
        return self._props.get(prop_id)


class _FakeMechanicalMaterial:
    """A material with Fusion's mechanical property ids and units (a blue template's values)."""

    def __init__(self) -> None:
        self.description = "Filamatch | x"
        self.props = {
            "structural_Behavior": _FakeMechanicalProperty("structural_transverse_isotropic", ""),
            "structural_Young_modulus": _FakeMechanicalProperty(6890000.0, "Kilopascal"),
            "structural_Poisson_ratio": _FakeMechanicalProperty(0.34, "Unitless"),
            "structural_Shear_modulus": _FakeMechanicalProperty(2570000.0, "Kilopascal"),
            "structural_Minimum_yield_stress": _FakeMechanicalProperty(51710.0, "Kilopascal"),
            "structural_Minimum_tensile_strength": _FakeMechanicalProperty(51710.0, "Kilopascal"),
        }
        self.materialProperties = _FakeMechanicalProperties(self.props)


def _check_solid_values() -> None:
    """Data sheet first, then the type's generic values; no data or no properties: unchanged."""
    paht = FakeCatalogEntry("bambu_paht_cf_black", "#161616", category="PA-CF", product_line="PAHT-CF",
                            slicer_filament_type="PA-CF")
    material = _FakeMechanicalMaterial()
    _set_solid_values(material, paht, "Filamatch | PA-CF | Bambu Lab - PAHT-CF - Black", "test")
    props = material.props
    _expect(abs(props["structural_Young_modulus"].value - 3020000.0) < 1.0,
            "PAHT-CF: typical E = mean of the sheet's XY 3860 and Z 2180 MPa, in kPa, got {}".format(
                props["structural_Young_modulus"].value))
    _expect(abs(props["structural_Minimum_tensile_strength"].value - 69500.0) < 1.0,
            "PAHT-CF: typical tensile = mean of 92 and 47 MPa, in kPa")
    poisson = props["structural_Poisson_ratio"].value
    _expect(abs(props["structural_Shear_modulus"].value - 3020000.0 / (2 * (1 + poisson))) < 1.0,
            "shear modulus follows E and Poisson's ratio")
    _expect(props["structural_Behavior"].value == "structural_isotropic", "the values are isotropic")
    _expect(material.description.endswith(SOLID_VALUES_NOTE + "Bambu Lab PAHT-CF (data sheet V3.0)"),
            "the description names the data sheet, got {!r}".format(material.description))
    _expect(material.description.startswith("Filamatch | PA-CF | Bambu Lab - PAHT-CF - Black | "),
            "the description keeps its first parts")

    pla = _FakeMechanicalMaterial()
    _set_solid_values(pla, FakeCatalogEntry("bambu_pla_basic_black", "#161616"), "Filamatch | PLA | y", "test")
    _expect(pla.description == "Filamatch | PLA | y | " + SOLID_VALUES_NOTE + "PLA (generic)",
            "a filament without a data sheet gets its type's generic values, got {!r}".format(pla.description))
    _expect(1000000.0 < pla.props["structural_Young_modulus"].value < 4000000.0,
            "generic PLA stiffness is 1-4 GPa")

    unknown = _FakeMechanicalMaterial()
    _set_solid_values(unknown, FakeCatalogEntry("x_y", "#161616", category="WOOD", manufacturer="Nobody",
                                                product_line="Oak", slicer_filament_type="WOOD"),
                      "Filamatch | WOOD | z", "test")
    _expect(unknown.props["structural_Young_modulus"].value == 6890000.0
            and unknown.description == "Filamatch | WOOD | z | no solid values (no data for this filament)",
            "a filament without data keeps its values and says so, got {!r}".format(unknown.description))
    _expect("solid values:" not in unknown.description, "the printed values must not take it for solid values")

    tpu = _FakeMechanicalMaterial()
    _set_solid_values(tpu, FakeCatalogEntry("bambu_tpu_95a_hf_grey", "#808080", category="TPU",
                                            product_line="TPU 95A HF", slicer_filament_type="TPU"),
                      "Filamatch | TPU | Bambu Lab - TPU 95A HF - Grey", "test")
    _expect(tpu.props["structural_Young_modulus"].value == 6890000.0
            and tpu.description.endswith("| no solid values (flexible filament)"),
            "a flexible filament gets no solid values, got {!r}".format(tpu.description))

    bare = FakeLibraryMaterial("bare")
    _set_solid_values(bare, paht, "Filamatch | PA-CF | w", "test")  # no materialProperties: no error


def _check_shared_body() -> None:
    """Wrappers of one native body (shared component) are relinked and counted once."""
    from . import materials_workflow

    template = _make_template_material()
    entry = _make_entry()
    gray = FakeDesignMaterial(
        entry.display_name, "design-gray", FakeAppearance("Gray", FakeColor(0x80, 0x80, 0x80))
    )
    native = FakeBody("Shared Body", gray)
    shared = FakeComponent("Shared")
    shared.bRepBodies = materials_workflow.FakeViewCollection([native], True)
    design = FakeDesign(
        root_component=FakeComponent(
            "Root",
            occurrences=[FakeOccurrence("Shared:1", shared), FakeOccurrence("Shared:2", shared)],
        ),
        materials=FakeDesignMaterials([gray]),
    )
    gray.delete_guard = lambda: native.material is gray
    result = ensure_favorite_filament(FakeApp(), design, entry, template_material=template)
    _expect(result.relinked_bodies == 1, "a shared body must be counted once, got {}".format(result.relinked_bodies))
    _expect(native.material is result.design_material, "the shared body must be relinked")
    _expect(result.design_material.name == entry.display_name, "the copy must take the final name")


def _selftest() -> int:
    """Run the Favorites workflow self-test against fake Fusion objects.

    Returns:
        Process exit code: 0 when every check passed, 1 otherwise.
    """
    scenarios = (
        ("color_hex converts Fusion colors and validates input", _check_color_hex),
        (
            "favorites_summary and find_favorite report the Favorites library",
            _check_summary_and_lookup,
        ),
        (
            "ensure_favorite_filament creates a colored favorite and design material",
            _check_clean_import,
        ),
        ("repeated imports reuse favorites and design materials", _check_favorite_reuse),
        ("ensure_favorite_filament migrates a gray material and relinks bodies", _check_migration),
        (
            "migration works without another design material",
            _check_migration_without_placeholder,
        ),
        ("missing template/importer raise actionable FavoritesError", _check_missing_template),
        ("import_catalog_to_favorites imports batches and fails fast", _check_import_batch),
        (
            "favorites copy the nearest-coloured template and the catalog density",
            _check_swatch_templates,
        ),
        (
            "stale_swatch_favorites lists favorites with an old template swatch",
            _check_stale_swatch_favorites,
        ),
        (
            "design copies with an old template are replaced, id twins untouched",
            _check_design_template_migration,
        ),
        (
            "old favorites are renamed OLD - and replaced by colour-template ones",
            _check_old_favorite_replacement,
        ),
        (
            "replace_stale_favorites replaces every stale favorite in Favorites only",
            _check_replace_stale_favorites,
        ),
        ("a foreign favorite appearance is never recoloured", _check_foreign_appearance),
        ("a failed favorite add keeps the old favorite names", _check_failed_add_keeps_names),
        ("a failed migration leaves the design unchanged", _check_migration_rollback),
        ("a body in a component used twice is relinked and counted once", _check_shared_body),
        ("filament materials get their own solid values, not the template's", _check_solid_values),
    )
    try:
        for label, scenario in scenarios:
            scenario()
            print("PASS: {}".format(label))
    except Exception:
        traceback.print_exc()
        print("FAIL: favorites self-test aborted", file=sys.stderr)
        return 1
    print("PASS: all favorites self-tests completed")
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.CRITICAL, format="%(levelname)s %(name)s: %(message)s")
    sys.exit(_selftest())
