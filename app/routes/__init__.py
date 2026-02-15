from .account import account_bp
from .api import api_bp
from .site import site_bp


def register_blueprints(app):
    app.register_blueprint(site_bp)
    app.register_blueprint(account_bp)
    app.register_blueprint(api_bp)
