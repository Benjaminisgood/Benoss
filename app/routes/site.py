from flask import Blueprint, current_app, flash, g, redirect, render_template, request, session, url_for

from ..extensions import db
from ..models import User
from ..utils.runtime_settings import get_setting_int
from ..utils.session_auth import login_required, login_user, logout_user, safe_next_url


site_bp = Blueprint("site", __name__)


@site_bp.route("/")
@login_required()
def home():
    return render_template("home.html", page="home", title="Benoss Home")


@site_bp.route("/board")
@login_required()
def board():
    config_default = int(current_app.config.get("BOARD_DEFAULT_DAYS") or 7)
    board_default_days = get_setting_int("BOARD_DEFAULT_DAYS", default=config_default)
    board_default_days = min(max(board_default_days, 1), 30)
    return render_template("board.html", page="board", title="Board", board_default_days=board_default_days)


@site_bp.route("/echoes")
@login_required()
def echoes():
    return render_template("echoes.html", page="echoes", title="Echoes")


@site_bp.route("/notice")
@login_required()
def notice():
    return render_template("notice.html", page="notice", title="Notice")


@site_bp.route("/admin")
@login_required(role="admin")
def admin():
    return render_template("admin.html", page="admin", title="Admin")


@site_bp.route("/login", methods=["GET", "POST"])
def login():
    if g.get("user"):
        return redirect(url_for("site.home"))

    next_url = safe_next_url(request.args.get("next", ""))
    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        remember = request.form.get("remember") == "on"

        user = User.query.filter_by(username=username, is_active=True).first()
        if user and user.check_password(password):
            login_user(user)
            session.permanent = remember
            return redirect(next_url or url_for("site.home"))

        flash("用户名或密码错误", "error")

    return render_template("login.html", page="login", title="Login", next_url=next_url)


@site_bp.route("/register", methods=["GET", "POST"])
def register():
    if g.get("user"):
        return redirect(url_for("site.home"))

    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        password_confirm = request.form.get("password_confirm") or ""

        if not username:
            flash("用户名不能为空", "error")
        elif len(username) < 3 or len(username) > 40:
            flash("用户名长度需在 3-40 之间", "error")
        elif password != password_confirm:
            flash("两次输入的密码不一致", "error")
        elif len(password) < 6:
            flash("密码至少 6 位", "error")
        elif User.query.filter_by(username=username).first():
            flash("用户名已存在", "error")
        else:
            user = User(username=username, role="user", is_active=True)
            user.set_password(password)
            db.session.add(user)
            db.session.commit()
            flash("注册成功，请登录", "success")
            return redirect(url_for("site.login"))

    return render_template("register.html", page="register", title="Register")


@site_bp.route("/logout")
def logout():
    logout_user()
    return redirect(url_for("site.login"))
