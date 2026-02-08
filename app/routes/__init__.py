from .account import account_bp
from .admin import admin_bp
from .dailyreel import dailyreel_bp
from .links import links_bp
from .projects import projects_bp
from .site import site_bp
from .whiteboard import whiteboard_bp


def register_blueprints(app):
    app.register_blueprint(site_bp)
    app.register_blueprint(account_bp)
    app.register_blueprint(projects_bp)
    app.register_blueprint(whiteboard_bp)
    app.register_blueprint(dailyreel_bp)
    app.register_blueprint(links_bp)
    app.register_blueprint(admin_bp)
