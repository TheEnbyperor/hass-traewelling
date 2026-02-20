import voluptuous as vol
import yarl
import secrets
import hashlib
import base64
import datetime
from typing import Any
from homeassistant import config_entries
from homeassistant.components import webhook
from homeassistant.core import callback
from homeassistant.helpers import config_entry_oauth2_flow, aiohttp_client
from . import const


class ConfigFlow(config_entries.ConfigFlow, domain=const.DOMAIN):
    VERSION = 1
    SUBVERSION = 1

    async def async_step_user(
            self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        if user_input is None:
            return self.async_show_form(
                step_id="user",
                data_schema=vol.Schema({
                    vol.Required("name", default="Träwelling"): str,
                    vol.Required("instance_url", default=const.DEFAULT_ROOT_URL): str,
                    vol.Required("oauth_client_id", default=const.DEFAULT_OAUTH_CLIENT_ID): str,
                }),
            )

        await self.async_set_unique_id(user_input["instance_url"])
        self._abort_if_unique_id_configured()

        user_input["traewelling_instance_id"] = webhook.async_generate_id()

        return self.async_create_entry(
            title=user_input["name"],
            data=user_input,
        )

    @classmethod
    @callback
    def async_get_supported_subentry_types(
            cls, config_entry: config_entries.ConfigEntry
    ) -> dict[str, type[config_entries.ConfigSubentryFlow]]:
        return {
            "account": AccountFlow
        }


class AccountFlow(config_entries.ConfigSubentryFlow):
    async def async_step_user(
            self, user_input: dict[str, Any] | None = None
    ) -> config_entries.SubentryFlowResult:
        if user_input is not None:
            self.data = user_input
            next_step = "authorize_rejected" if "error" in user_input else "link"
            return self.async_external_step_done(next_step_id=next_step)

        self.entry = self._get_entry()
        self.code_verifier = secrets.token_urlsafe(96)
        self.redirect_uri = config_entry_oauth2_flow.async_get_redirect_uri(self.hass)
        hashed_code_verifier = hashlib.sha256(self.code_verifier.encode("ascii")).digest()
        code_challenge = base64.urlsafe_b64encode(hashed_code_verifier).decode("ascii").replace("=", "")
        redirect_url = str(yarl.URL(f"{self.entry.data['instance_url']}/oauth/authorize").with_query({
            "response_type": "code",
            "client_id": self.entry.data["oauth_client_id"],
            "redirect_uri": self.redirect_uri,
            "state": config_entry_oauth2_flow._encode_jwt(self.hass, {
                "flow_id": self.flow_id
            }),
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            "scope": "read-statuses read-notifications",
        }))

        return self.async_external_step(step_id="user", url=redirect_url)

    async def async_step_authorize_rejected(
            self, data: None = None
    ) -> config_entries.ConfigFlowResult:
        return self.async_abort(reason="user_rejected_authorize")

    async def async_step_link(
            self, data: None = None
    ) -> config_entries.ConfigFlowResult:
        session = aiohttp_client.async_get_clientsession(self.hass)
        resp = await session.post(
            f"{self.entry.data['instance_url']}/oauth/token",
            json={
                "grant_type": "authorization_code",
                "client_id": self.entry.data["oauth_client_id"],
                "code": self.data["code"],
                "code_verifier": self.code_verifier,
                "redirect_uri": self.redirect_uri,
            }
        )
        if not resp.ok:
            return self.async_abort(reason="oauth_error")

        data = await resp.json()
        if data["token_type"] != "Bearer":
            return self.async_abort(reason="oauth_error")

        access_token = data["access_token"]
        refresh_token = data["refresh_token"]
        expires_at = datetime.datetime.utcnow() + datetime.timedelta(seconds=int(data["expires_in"]))

        resp = await session.get(
            f"{self.entry.data['instance_url']}/api/v1/auth/user",
            headers={
                "Authorization": f"Bearer {access_token}",
            }
        )
        if not resp.ok:
            return self.async_abort(reason="oauth_error")
        user_data = await resp.json()

        return self.async_create_entry(
            title=user_data["data"]["displayName"],
            data={
                "token": access_token,
                "refresh_token": refresh_token,
                "expires_at": expires_at.isoformat(),
            },
            unique_id=str(user_data["data"]["id"]),
        )