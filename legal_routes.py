"""
利用規約・プライバシーポリシーの単独ページ（/terms・/privacy）。
====================================================================
本文はトップのモーダル（templates/index.html）と同じ部品（templates/_legal_terms_body.html・_legal_privacy_body.html）を読む。
シミュレーターなど、トップのモーダルが無いページから規約を開けるようにするため（2026-10-29 規約の改定）。
"""

from flask import Blueprint, render_template

legal_bp = Blueprint("legal", __name__)


@legal_bp.route("/terms")
def terms_page():
    return render_template("legal_page.html", title="利用規約", body_template="_legal_terms_body.html")


@legal_bp.route("/privacy")
def privacy_page():
    return render_template("legal_page.html", title="プライバシーポリシー", body_template="_legal_privacy_body.html")
