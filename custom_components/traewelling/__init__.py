import datetime
from aiohttp import web
from aiohttp.web_urldispatcher import PlainResource
from homeassistant.core import HomeAssistant
from homeassistant.components import http
from homeassistant.config_entries import ConfigEntry
from homeassistant.data_entry_flow import UnknownFlow
from homeassistant.helpers import config_entry_oauth2_flow
from homeassistant.helpers.typing import ConfigType
from homeassistant.helpers.dispatcher import async_dispatcher_send
from .const import DOMAIN

def _signal_sync(entry_id: str) -> str:
    return f"{DOMAIN}_{entry_id}_sync_entities"

async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    old_res = next(filter(
        lambda r: r.canonical == config_entry_oauth2_flow.AUTH_CALLBACK_PATH,
        hass.http.app.router.resources()
    ))
    hass.http.app.router.unindex_resource(old_res)
    hass.http.register_view(OAuth2AuthorizeCallbackView())
    return True

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:

    async def _handle_entry_update(hass: HomeAssistant, updated_entry: ConfigEntry) -> None:
        async_dispatcher_send(hass, _signal_sync(updated_entry.entry_id))

    entry.async_on_unload(entry.add_update_listener(_handle_entry_update))

    await hass.config_entries.async_forward_entry_setups(entry, ["sensor"])
    return True

async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    await hass.config_entries.async_forward_entry_unload(entry, "sensor")
    return True

async def async_remove_config_entry_device(
    hass: HomeAssistant, entry: ConfigEntry, device_entry: DeviceEntry
) -> bool:
    await hass.config_entries.async_forward_entry_unload(entry, "sensor")
    return True


class OAuth2AuthorizeCallbackView(http.HomeAssistantView):
    requires_auth = False
    url = config_entry_oauth2_flow.AUTH_CALLBACK_PATH
    name = "traewelling:auth_callback"

    async def get(self, request: web.Request) -> web.Response:
        if "state" not in request.query:
            return web.Response(text="Missing state parameter")

        hass = request.app[http.KEY_HASS]

        state = config_entry_oauth2_flow._decode_jwt(hass, request.query["state"])

        if state is None:
            return web.Response(
                text="Invalid state. Is My Home Assistant configure to go to the right instance?",
                status=400,
            )

        user_input: dict[str, Any] = {"state": state}

        if "code" in request.query:
            user_input["code"] = request.query["code"]
        elif "error" in request.query:
            user_input["error"] = request.query["error"]
        else:
            return web.Response(text="Missing code or error parameter")

        try:
            await hass.config_entries.flow.async_configure(
                flow_id=state["flow_id"], user_input=user_input
            )
        except UnknownFlow:
            await hass.config_entries.subentries.async_configure(
                flow_id=state["flow_id"], user_input=user_input
            )
        return web.Response(
            headers={"content-type": "text/html"},
            text="<script>window.close()</script>",
        )
