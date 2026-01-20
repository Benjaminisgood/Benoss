from .account import account_bp
from .admin import admin_bp
from .album import album_bp
from .blog import blog_bp
from .everyday import everyday_bp
from .note import note_bp
from .site import site_bp


def register_blueprints(app):
    app.register_blueprint(site_bp)
    app.register_blueprint(blog_bp)
    app.register_blueprint(note_bp)
    app.register_blueprint(everyday_bp)
    app.register_blueprint(album_bp)
    app.register_blueprint(admin_bp)
    app.register_blueprint(account_bp)
