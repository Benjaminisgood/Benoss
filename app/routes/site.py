from flask import Blueprint, g, redirect, render_template, request, url_for

from ..models import User
from ..utils.session_auth import login_required, login_user, logout_user, safe_next_url


site_bp = Blueprint("site", __name__)


@site_bp.route("/")
@login_required()
def home():
    return render_template("index.html", page="home", title="Benoss")


@site_bp.route("/blog")
@login_required()
def blog():
    return render_template("blog.html", page="blog", title="Blog")


@site_bp.route("/note")
@login_required()
def note():
    return render_template("note.html", page="note", title="Note")


@site_bp.route("/everyday")
@login_required()
def everyday():
    return render_template("everyday_view.html", page="everyday-view", title="Everyday")


@site_bp.route("/everyday/manage")
@login_required(role="admin")
def everyday_manage():
    return render_template("everyday_manage.html", page="everyday-manage", title="Everyday Manage")


@site_bp.route("/album")
@login_required()
def album():
    return render_template("album.html", page="album", title="Attachments")


@site_bp.route("/admin")
@login_required(role="admin")
def admin():
    return render_template("admin.html", page="admin", title="Admin")


@site_bp.route("/login", methods=["GET", "POST"])
def login():
    next_url = safe_next_url(request.args.get("next", ""))
    error = None
    if g.get("user"):
        return redirect(next_url or url_for("site.home"))
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        if not username or not password:
            error = "Missing credentials"
        else:
            user = User.query.filter_by(username=username, is_active=True).first()
            if user and user.check_password(password):
                login_user(user)
                return redirect(next_url or url_for("site.home"))
            error = "Invalid username or password"
    return render_template("login.html", page="login", title="Login", error=error, next_url=next_url)


@site_bp.route("/logout")
def logout():
    logout_user()
    return redirect(url_for("site.login"))
