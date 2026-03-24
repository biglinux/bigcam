"""Resource-warning dialog for BigCam.

Shows an Adw.AlertDialog when the ResourceMonitor emits ``high-resource``,
listing active features that are probably causing high CPU/RAM usage and
offering an "Optimize" action to disable the heaviest ones.

Includes a "Don't show again" checkbox whose state is persisted in
SettingsManager.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gtk  # noqa: E402

from utils.i18n import _  # noqa: E402

if TYPE_CHECKING:
    from core.resource_monitor import FeatureDescriptor, ResourceSnapshot
    from utils.settings_manager import SettingsManager

log = logging.getLogger(__name__)

# Settings key – list of feature_ids the user dismissed permanently.
DISMISSED_KEY = "resource-warnings-dismissed"
MONITOR_ENABLED_KEY = "resource-monitor-enabled"


def show_resource_warning(
    parent: Gtk.Window,
    snapshot: "ResourceSnapshot",
    features: list["FeatureDescriptor"],
    settings: "SettingsManager",
    *,
    present_fn=None,
    on_optimized: "Callable[[list[str]], None] | None" = None,
) -> None:
    """Present a resource-warning dialog to the user.

    Parameters
    ----------
    parent : Gtk.Window
        The parent window (for modality).
    snapshot : ResourceSnapshot
        Current resource usage.
    features : list[FeatureDescriptor]
        Active features sorted by cost (highest first).
    settings : SettingsManager
        For reading/writing dismissed warnings.
    present_fn : callable, optional
        Custom present function (e.g. ``immersion.present_dialog``).
        Falls back to ``dialog.present(parent)``.
    on_optimized : callable, optional
        Called after features are disabled, with the list of disabled
        feature IDs so the caller can sync the UI.
    """
    # Filter out features the user chose to permanently ignore.
    dismissed: list = settings.get(DISMISSED_KEY, [])
    if not isinstance(dismissed, list):
        dismissed = []
    actionable = [f for f in features if f.feature_id not in dismissed]
    if not actionable:
        log.debug("All high-resource features dismissed by user – skipping dialog")
        return

    can_disable = [f for f in actionable if f.disableable]

    # ── Build the dialog ─────────────────────────────────────────────
    title = _("High resource usage detected")
    ram_info = f"{snapshot.rss_mb:.0f} MB RAM"
    cpu_info = f"{snapshot.cpu_percent:.0f}% CPU"
    body_parts: list[str] = [
        _("The application is using %s and %s.") % (ram_info, cpu_info),
        "",
        _("Active features that may be causing this:"),
    ]
    for feat in actionable:
        suffix = "" if feat.disableable else f" ({_('active source')})"
        body_parts.append(f"  • {feat.label}{suffix}")
    body_parts.append("")
    if can_disable:
        body_parts.append(
            _("You can optimize by disabling the heaviest features, "
              "or continue if you understand the impact.")
        )
    else:
        body_parts.append(
            _("No features can be disabled (active camera sources "
              "cannot be stopped from here).")
        )
    body = "\n".join(body_parts)

    dialog = Adw.AlertDialog.new(title, body)

    # ── Checkbox: "Don't show again" ─────────────────────────────────
    check = Gtk.CheckButton(label=_("Don't show this warning again"))
    check.set_margin_top(8)
    check.update_property(
        [Gtk.AccessibleProperty.LABEL],
        [_("Suppress resource usage warnings permanently")],
    )
    dialog.set_extra_child(check)

    # ── Responses ────────────────────────────────────────────────────
    dialog.add_response("continue", _("I understand, continue"))
    if can_disable:
        dialog.add_response("optimize", _("Optimize (disable heavy features)"))
        dialog.set_response_appearance(
            "optimize", Adw.ResponseAppearance.SUGGESTED
        )
        dialog.set_default_response("optimize")
    dialog.set_close_response("continue")

    def _on_response(_dlg: Adw.AlertDialog, response: str) -> None:
        if check.get_active():
            # Store dismissed feature IDs so we don't prompt again.
            new_dismissed = list(set(dismissed + [f.feature_id for f in actionable]))
            settings.set(DISMISSED_KEY, new_dismissed)

        if response == "optimize":
            disabled_ids: list[str] = []
            for feat in can_disable:
                try:
                    feat.disable()
                    disabled_ids.append(feat.feature_id)
                    log.info("Disabled feature '%s' to reduce resource usage", feat.feature_id)
                except Exception:
                    log.exception("Failed to disable feature '%s'", feat.feature_id)
            if on_optimized and disabled_ids:
                on_optimized(disabled_ids)

    dialog.connect("response", _on_response)

    if present_fn:
        present_fn(dialog, parent)
    else:
        dialog.present(parent)
