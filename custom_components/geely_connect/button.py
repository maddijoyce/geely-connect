"""Geely one-shot action buttons.

  Find car        → RHL / [{rhl: "horn-light-flash"}]     verified live
  Unlock Trunk    → RDU_2 / [{target: "trunk"}]
      Unlocks the tailgate LATCH. It does not open the tailgate electrically -
      the gate still has to be lifted by hand, and it re-locks itself after a
      short window if nobody does. Four owners across three trims have confirmed
      the official app does the same, so this is the whole of the feature rather
      than an approximation of it; see const.py. The label "AVD-verified
      2026-05-01" used to appear here; it predates this repository's public
      history and nobody can produce the capture, so it is gone rather than left
      to lend false confidence.

Rapid warming / cooling are exposed as climate.preset_modes - see
climate.py - because they're a "set the climate to a mode" action, not
a true one-shot, so the preset UX is cleaner.
"""
# -----------------------------------------------------------------------------
# Portions of this file - the reverse-engineered Geely protocol / field mappings
# (the parts that required protocol research) - are derived from
# nitaybz/geely-global-ha, used under the MIT License. See NOTICE.txt.
# Original framework, security hardening and transport are our own work.
# -----------------------------------------------------------------------------
from __future__ import annotations

import logging

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_call_later

from .api import GeelyControlError, redact

from .const import (
    DOMAIN,
    POSITION_SETTLE_SECONDS,
    SERVICE_FIND_CAR,
    SERVICE_FIND_CAR_PARAMS,
    SERVICE_TAILGATE,
    SERVICE_TAILGATE_PARAMS,
)

_LOGGER = logging.getLogger(__name__)


# Standard telematics buttons (PUT /remote-control/vehicle/telematics/{VIN})
# (key, name, icon, service_id, params, capability_flag_or_None)
SIMPLE_BUTTONS: list[tuple[str, str, str, str, list[dict], str | None]] = [
    ("find_car",     "Find Car",     "mdi:car-search", SERVICE_FIND_CAR,  SERVICE_FIND_CAR_PARAMS, "find_car.enabled"),
    ("unlock_trunk", "Unlock Trunk", "mdi:car-back",   SERVICE_TAILGATE,  SERVICE_TAILGATE_PARAMS, "tailgate.enabled"),
]

# Note: rapid warming / rapid cooling are exposed as climate presets
# (see climate.py), not as separate buttons. They're a "set the climate
# to a mode" action, not a true one-shot, so the preset UX is cleaner.


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, add_entities: AddEntitiesCallback) -> None:
    bundle = hass.data[DOMAIN][entry.entry_id]
    caps = bundle.get("capabilities") or {}

    entities: list[ButtonEntity] = []
    for key, name, icon, sid, params, flag in SIMPLE_BUTTONS:
        if flag and not caps.get(flag, True):
            _LOGGER.debug("button %s skipped (capability flag %s=False)", key, flag)
            continue
        entities.append(GeelyTelematicsButton(hass, bundle, key, name, icon, sid, params))

    # Manual "Refresh now" - forces an immediate poll, bypassing the back-off.
    entities.append(GeelyRefreshButton(hass, bundle))

    add_entities(entities)


class GeelyRefreshButton(ButtonEntity):
    """Fetch fresh vehicle data right now, regardless of the polling interval."""

    _attr_has_entity_name = True
    _attr_icon = "mdi:refresh"

    def __init__(self, hass: HomeAssistant, bundle: dict) -> None:
        self._hass = hass
        self._coordinator = bundle["coordinator"]
        self._poll_state = bundle.get("poll_state")
        vin = bundle["vin"]
        self._attr_unique_id = f"geely_{vin}_btn_refresh"
        self._attr_name = "Refresh Data"
        # Cancel handle for the pending follow-up read (see async_press).
        self._cancel_resync = None
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, vin)},
            manufacturer="Geely",
            name=bundle.get("device_name") or f"Geely ({vin})",
        )

    async def async_press(self) -> None:
        """Fetch now and tell the user if it failed.

        async_request_refresh() is debounced and returns without waiting, which
        is right for the automatic post-command polls but wrong here: in Manual
        polling mode this button is the only thing that fetches data, so a
        press has to actually run the poll and surface a failure instead of
        silently leaving stale values on screen.

        It also asks for the secondary endpoints, which the timer only fetches
        every few cycles: a press means "everything, now", and without this
        three presses in four left the vehicle-state block untouched (#4).
        """
        if self._poll_state is not None:
            self._poll_state["force_secondary"] = True
        await self._coordinator.async_refresh()
        if not self._coordinator.last_update_success:
            err = self._coordinator.last_exception
            raise HomeAssistantError(
                f"Geely sync failed: {err}" if err else "Geely sync failed"
            )
        # The press woke the car for a fresh GPS fix, but that fix is uploaded
        # seconds after the gateway ACKs the wake - so the read that just
        # finished is one step behind the wake it sent, and every press showed
        # the position from BEFORE it. Come back once to collect the fix.
        #
        # The follow-up asks for nothing: the force flag was spent above, so
        # this cannot send a second wake, and on a car that never woke it is a
        # cheap ordinary poll. It matters most in Manual mode, where there is
        # no later poll to carry the fix instead.
        self._schedule_resync()

    def _schedule_resync(self) -> None:
        """Queue the follow-up read, replacing any still pending."""
        self._cancel_pending()
        self._cancel_resync = async_call_later(
            self._hass, POSITION_SETTLE_SECONDS, self._async_resync)

    def _cancel_pending(self) -> None:
        if self._cancel_resync is not None:
            self._cancel_resync()
            self._cancel_resync = None

    async def _async_resync(self, _now) -> None:
        """Read again, now that the car has had time to upload its fix."""
        self._cancel_resync = None
        await self._coordinator.async_request_refresh()

    async def async_will_remove_from_hass(self) -> None:
        """A reload must not leave a timer pointing at a dead coordinator."""
        self._cancel_pending()


class GeelyTelematicsButton(ButtonEntity):
    _attr_has_entity_name = True

    def __init__(self, hass: HomeAssistant, bundle: dict, key: str, name: str,
                 icon: str, service_id: str, params: list[dict]) -> None:
        self._hass = hass
        self._api = bundle["api"]
        self._vin = bundle["vin"]
        self._service_id = service_id
        self._params = params
        self._attr_unique_id = f"geely_{self._vin}_btn_{key}"
        self._attr_name = name
        self._attr_icon = icon
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, self._vin)},
            manufacturer="Geely",
            name=bundle.get("device_name") or f"Geely ({self._vin})",
        )

    async def async_press(self) -> None:
        try:
            resp = await self._hass.async_add_executor_job(
                self._api.control, self._service_id, self._params,
            )
        except GeelyControlError as e:
            raise HomeAssistantError(f"Geely {self._service_id}: {e.message}") from e
        except Exception as e:
            _LOGGER.exception("Button %s failed", self._service_id)
            raise HomeAssistantError(f"Geely {self._service_id} failure: {e}") from e
        _LOGGER.debug("Geely button %s response: %s", self._service_id, redact(resp))


