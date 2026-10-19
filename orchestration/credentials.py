from integration.auth import hash_password
from integration.config_file import save_security

def password_hash(password):
    return hash_password(password)

def configure_credentials(settings,password):
    import secrets
    save_security(settings.config_file,hash_password(password),secrets.token_urlsafe(48))
