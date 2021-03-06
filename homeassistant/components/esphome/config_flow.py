"""Config flow to configure esphome component."""
from __future__ import annotations

from collections import OrderedDict
from typing import Any

from aioesphomeapi import APIClient, APIConnectionError, DeviceInfo
import voluptuous as vol

from homeassistant.components import zeroconf
from homeassistant.config_entries import ConfigFlow
from homeassistant.const import CONF_HOST, CONF_NAME, CONF_PASSWORD, CONF_PORT
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers.typing import DiscoveryInfoType

from . import DOMAIN, DomainData


class EsphomeFlowHandler(ConfigFlow, domain=DOMAIN):
    """Handle a esphome config flow."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize flow."""
        self._host: str | None = None
        self._port: int | None = None
        self._password: str | None = None

    async def _async_step_user_base(
        self, user_input: dict[str, Any] | None = None, error: str | None = None
    ) -> FlowResult:
        if user_input is not None:
            return await self._async_authenticate_or_add(user_input)

        fields: dict[Any, type] = OrderedDict()
        fields[vol.Required(CONF_HOST, default=self._host or vol.UNDEFINED)] = str
        fields[vol.Optional(CONF_PORT, default=self._port or 6053)] = int

        errors = {}
        if error is not None:
            errors["base"] = error

        return self.async_show_form(
            step_id="user", data_schema=vol.Schema(fields), errors=errors
        )

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle a flow initialized by the user."""
        return await self._async_step_user_base(user_input=user_input)

    @property
    def _name(self) -> str | None:
        return self.context.get(CONF_NAME)

    @_name.setter
    def _name(self, value: str) -> None:
        self.context[CONF_NAME] = value
        self.context["title_placeholders"] = {"name": self._name}

    def _set_user_input(self, user_input: dict[str, Any] | None) -> None:
        if user_input is None:
            return
        self._host = user_input[CONF_HOST]
        self._port = user_input[CONF_PORT]

    async def _async_authenticate_or_add(
        self, user_input: dict[str, Any] | None
    ) -> FlowResult:
        self._set_user_input(user_input)
        error, device_info = await self.fetch_device_info()
        if error is not None:
            return await self._async_step_user_base(error=error)
        assert device_info is not None
        self._name = device_info.name

        # Only show authentication step if device uses password
        if device_info.uses_password:
            return await self.async_step_authenticate()

        return self._async_get_entry()

    async def async_step_discovery_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle user-confirmation of discovered node."""
        if user_input is not None:
            return await self._async_authenticate_or_add(None)
        return self.async_show_form(
            step_id="discovery_confirm", description_placeholders={"name": self._name}
        )

    async def async_step_zeroconf(
        self, discovery_info: DiscoveryInfoType
    ) -> FlowResult:
        """Handle zeroconf discovery."""
        # Hostname is format: livingroom.local.
        local_name = discovery_info["hostname"][:-1]
        node_name = local_name[: -len(".local")]
        address = discovery_info["properties"].get("address", local_name)

        # Check if already configured
        await self.async_set_unique_id(node_name)
        self._abort_if_unique_id_configured(
            updates={CONF_HOST: discovery_info[CONF_HOST]}
        )

        for entry in self._async_current_entries():
            already_configured = False

            if CONF_HOST in entry.data and entry.data[CONF_HOST] in [
                address,
                discovery_info[CONF_HOST],
            ]:
                # Is this address or IP address already configured?
                already_configured = True
            elif DomainData.get(self.hass).is_entry_loaded(entry):
                # Does a config entry with this name already exist?
                data = DomainData.get(self.hass).get_entry_data(entry)

                # Node names are unique in the network
                if data.device_info is not None:
                    already_configured = data.device_info.name == node_name

            if already_configured:
                # Backwards compat, we update old entries
                if not entry.unique_id:
                    self.hass.config_entries.async_update_entry(
                        entry,
                        data={**entry.data, CONF_HOST: discovery_info[CONF_HOST]},
                        unique_id=node_name,
                    )

                return self.async_abort(reason="already_configured")

        self._host = discovery_info[CONF_HOST]
        self._port = discovery_info[CONF_PORT]
        self._name = node_name

        return await self.async_step_discovery_confirm()

    @callback
    def _async_get_entry(self) -> FlowResult:
        assert self._name is not None
        return self.async_create_entry(
            title=self._name,
            data={
                CONF_HOST: self._host,
                CONF_PORT: self._port,
                # The API uses protobuf, so empty string denotes absence
                CONF_PASSWORD: self._password or "",
            },
        )

    async def async_step_authenticate(
        self, user_input: dict[str, Any] | None = None, error: str | None = None
    ) -> FlowResult:
        """Handle getting password for authentication."""
        if user_input is not None:
            self._password = user_input[CONF_PASSWORD]
            error = await self.try_login()
            if error:
                return await self.async_step_authenticate(error=error)
            return self._async_get_entry()

        errors = {}
        if error is not None:
            errors["base"] = error

        return self.async_show_form(
            step_id="authenticate",
            data_schema=vol.Schema({vol.Required("password"): str}),
            description_placeholders={"name": self._name},
            errors=errors,
        )

    async def fetch_device_info(self) -> tuple[str | None, DeviceInfo | None]:
        """Fetch device info from API and return any errors."""
        zeroconf_instance = await zeroconf.async_get_instance(self.hass)
        assert self._host is not None
        assert self._port is not None
        cli = APIClient(
            self.hass.loop,
            self._host,
            self._port,
            "",
            zeroconf_instance=zeroconf_instance,
        )

        try:
            await cli.connect()
            device_info = await cli.device_info()
        except APIConnectionError as err:
            if "resolving" in str(err):
                return "resolve_error", None
            return "connection_error", None
        finally:
            await cli.disconnect(force=True)

        return None, device_info

    async def try_login(self) -> str | None:
        """Try logging in to device and return any errors."""
        zeroconf_instance = await zeroconf.async_get_instance(self.hass)
        assert self._host is not None
        assert self._port is not None
        cli = APIClient(
            self.hass.loop,
            self._host,
            self._port,
            self._password,
            zeroconf_instance=zeroconf_instance,
        )

        try:
            await cli.connect(login=True)
        except APIConnectionError:
            await cli.disconnect(force=True)
            return "invalid_auth"

        return None
