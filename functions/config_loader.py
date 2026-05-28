import os
import json
from dotenv import load_dotenv

_config = None
USER_SECRETS_PATH = "/Users/spaceylamb/.secrets"

def load_config():
    global _config
    # Load API keys from project .env first, then user-level secrets without
    # overriding project-specific environment values.
    load_dotenv()
    load_dotenv(USER_SECRETS_PATH, override=False)
    api_keys = {
        "GEMINI_API_KEY": os.getenv("GEMINI_API_KEY"),
        "GOOGLE_CLOUD_PROJECT": os.getenv("GOOGLE_CLOUD_PROJECT"),
        "GOOGLE_APPLICATION_CREDENTIALS": os.getenv("GOOGLE_APPLICATION_CREDENTIALS"),
        "LUMALABS_API_KEY": os.getenv("LUMALABS_API_KEY"),
        "ARK_API_KEY": os.getenv("ARK_API_KEY"),
        "BYTEPLUS_ARK_KEY": os.getenv("BYTEPLUS_ARK_KEY"),
        "DAYONE_RELAY_TOKEN": os.getenv("DAYONE_RELAY_TOKEN"),
        "DAYONE_MAC_TOKEN": os.getenv("DAYONE_MAC_TOKEN"),
    }

    # Determine which config to load
    config_file = "config.json"

    with open(config_file, "r") as f:
        config = json.load(f)

    # Merge API keys into config
    config.update(api_keys)
    _config = config
    return config

def get_config():
    global _config
    if _config is None:
        return load_config()
    return _config
