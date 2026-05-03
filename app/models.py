from sqlalchemy import Column, String, DateTime, func
from .database import Base


class WidgetConfig(Base):
    """Per-widget configuration — one row per widget app registered on this backend."""
    __tablename__ = "widget_configs"

    widget_name = Column(String, primary_key=True)
    app_secret  = Column(String, nullable=False)   # MoySklad app secret for Vendor API calls
    app_uid     = Column(String, nullable=True)    # MoySklad appUid (optional, for reference)
    created_at  = Column(DateTime, server_default=func.now())
    updated_at  = Column(DateTime, server_default=func.now(), onupdate=func.now())


class AppToken(Base):
    """Stores MoySklad access tokens per widget + account."""
    __tablename__ = "app_tokens"

    widget_name = Column(String, primary_key=True)
    account_name = Column(String, primary_key=True)
    app_uid = Column(String, nullable=False)
    access_token = Column(String, nullable=False)
    account_id = Column(String, nullable=True, index=True)  # MoySklad account UUID
    installed_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())


class ContextSession(Base):
    """
    Short-lived mapping: contextKey → (widget_name, account_name).
    MoySklad generates a contextKey and passes it to the iframe URL.
    The frontend sends it here to get the access_token for its session.
    """
    __tablename__ = "context_sessions"

    context_key = Column(String, primary_key=True)
    widget_name = Column(String, nullable=False)
    account_name = Column(String, nullable=False)
    created_at = Column(DateTime, server_default=func.now())
