import base64
import sys
import time
from typing import Any, Callable, Dict, List, Optional, Union

import jsonschema
import jwt
import ujson as json
from cryptography.hazmat.backends.openssl.rsa import _RSAPublicKey

from consoleme.config import config
from consoleme.lib.crypto import Crypto
from consoleme.lib.plugins import get_plugin_by_name

crypto = Crypto()
stats = get_plugin_by_name(config.get("plugins.metrics"))()
log = config.get_logger()


async def generate_auth_token(
    user, ip, challenge_uuid, expiration=config.get("challenge.token_expiration", 3600)
):
    stats.count("generate_auth_token")
    log_data = {
        "user": user,
        "ip": ip,
        "function": f"{__name__}.{sys._getframe().f_code.co_name}",
        "message": "Generating token for user",
        "challenge_uuid": challenge_uuid,
    }
    log.debug(log_data)
    current_time = int(time.time())
    valid_before = current_time + expiration
    valid_after = current_time

    auth_token = {
        "user": user,
        "ip": ip,
        "challenge_uuid": challenge_uuid,
        "valid_before": valid_before,
        "valid_after": valid_after,
    }

    to_sign = (
        "{{'user': '{0}', 'ip': '{1}', 'challenge_uuid'': '{2}', "
        "'valid_before'': '{3}', 'valid_after'': '{4}'}}"
    ).format(user, ip, challenge_uuid, valid_before, valid_after)

    sig = crypto.sign(to_sign)

    auth_token["sig"] = sig
    return base64.b64encode(json.dumps(auth_token).encode())


async def validate_auth_token(user, ip, token):
    stats.count("validate_auth_token")
    log_data = {
        "user": user,
        "ip": ip,
        "function": f"{__name__}.{sys._getframe().f_code.co_name}",
        "message": "Validating token for user",
    }
    log.debug(log_data)
    if not token:
        stats.count("validate_auth_token.no_token")
        msg = f"No token passed. User: {user}. IP: {ip}."
        log.error(msg, exc_info=True)
        raise Exception(msg)
    decoded_token = base64.b64decode(token)
    auth_token = json.loads(decoded_token)
    current_time = int(time.time())

    if auth_token.get("user") != user:
        stats.count("validate_auth_token.user_mismatch")
        msg = f"Auth token has a different user: {auth_token.get('user')}. User passed to function: {user}"
        log.error(msg, exc_info=True)
        raise Exception(msg)

    if auth_token.get("ip") != ip:
        stats.count("validate_auth_token.ip_mismatch")
        msg = f"Auth token has a different IP: {auth_token.get('ip')}. IP passed to function: {ip}"
        log.error(msg, exc_info=True)
        raise Exception(msg)

    if (
        auth_token.get("valid_before") < current_time
        or auth_token.get("valid_after") > current_time
    ):
        stats.count("validate_auth_token.expiration_error")
        msg = (
            f"Auth token has expired. valid_before: {auth_token.get('valid_before')}. "
            f"valid_after: {auth_token.get('valid_after')}. Current_time: {current_time}"
        )
        log.error(msg, exc_info=True)
        raise Exception(msg)

    to_verify = (
        "{{'user': '{0}', 'ip': '{1}', 'challenge_uuid'': '{2}', "
        "'valid_before'': '{3}', 'valid_after'': '{4}'}}"
    ).format(
        auth_token.get("user"),
        auth_token.get("ip"),
        auth_token.get("challenge_uuid"),
        auth_token.get("valid_before"),
        auth_token.get("valid_after"),
    )

    token_details = {
        "valid": crypto.verify(to_verify, auth_token.get("sig")),
        "user": auth_token.get("user"),
    }

    return token_details


def can_edit_attributes(
    user: str, user_groups: List[str], group_info: Optional[Any]
) -> bool:
    for group in config.get("groups.can_admin", []):
        if group in user_groups:
            return True

    for group in config.get("groups.can_admin_restricted", []):
        if group in user_groups:
            return True

    for group in config.get("groups.can_edit_attributes", []):
        if group in user_groups:
            return True
    return False


def can_modify_members(
    user: str, user_groups: List[str], group_info: Optional[Any]
) -> bool:
    # No users can modify members on restricted groups
    if group_info and group_info.restricted:
        return False
    for group in config.get("groups.can_admin", []):
        if group in user_groups:
            return True

    for group in config.get("groups.can_admin_restricted", []):
        if group in user_groups:
            return True

    for group in config.get("groups.can_modify_members", []):
        if group in user_groups:
            return True

    return False


def can_edit_sensitive_attributes(
    user: str, user_groups: List[str], group_info: Optional[Any]
) -> bool:
    for group in config.get("groups.can_edit_sensitive_attributes", []):
        if group in user_groups:
            return True
        if user == group:
            return True
    return False


def is_sensitive_attr(attribute):
    for attr in config.get("groups.attributes.boolean", []):
        if attr.get("name") == attribute:
            return attr.get("sensitive", False)

    for attr in config.get("groups.attributes.list", []):
        if attr.get("name") == attribute:
            return attr.get("sensitive", False)
    return False


class Error(Exception):
    """Base class for exceptions in this module."""


class AuthenticationError(Error):
    """Exception raised for AuthN errors."""

    def __init__(self, message):
        self.message = message


def mk_jwt_validator(
    verification_str: _RSAPublicKey,
    header_cfg: Dict[str, Dict[str, List[str]]],
    payload_cfg: Dict[str, Dict[str, List[str]]],
) -> Callable:
    def validate_jwt(jwt_str):
        try:
            tkn = jwt.decode(
                jwt_str, verification_str, algorithms=header_cfg["alg"]["enum"]
            )
        except jwt.InvalidSignatureError:
            raise AuthenticationError("Invalid Token Signature")
        except jwt.ExpiredSignature:
            raise AuthenticationError("Token Expired")
        except jwt.InvalidAudienceError:
            raise AuthenticationError("Invalid Token Audience")
        except jwt.DecodeError:
            raise AuthenticationError("Invalid Token")
        except jwt.InvalidTokenError:
            raise AuthenticationError("Malformed Token")

        try:
            jsonschema.validate(tkn, payload_cfg)
        except jsonschema.ValidationError as e:
            raise AuthenticationError(e.message)
        return tkn

    return validate_jwt


class UnsupportedKeyTypeError(Error):
    """Exception raised unsupported JWK Errors."""

    def __init__(self, message):
        self.message = message


def mk_jwks_validator(
    jwk_set: List[Dict[str, Union[str, List[str]]]],
    header_cfg: Dict[str, Dict[str, List[str]]],
    payload_cfg: Dict[str, Dict[str, List[str]]],
) -> Callable:
    keys = []
    for j in jwk_set:
        if j["kty"] == "RSA":
            j_str = json.dumps(j)
            keys.append(jwt.algorithms.RSAAlgorithm.from_jwk(j_str))
        else:
            raise UnsupportedKeyTypeError("Unsupported Key Type: %s" % j["kty"])

    validators = [mk_jwt_validator(k, header_cfg, payload_cfg) for k in keys]

    def validate_jwt(jwt_str):
        result = None
        for v in validators:
            try:
                result = v(jwt_str)
            except AuthenticationError as e:
                result = e
            else:
                break
        if isinstance(result, Exception):
            raise result
        else:
            return result

    return validate_jwt