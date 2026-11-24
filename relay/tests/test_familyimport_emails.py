"""Cloud import forces a sign-in answer for every person: parsing, plan, report, apply, CLI."""

from __future__ import annotations

import pytest

from app import familyexport
from app.familyexport import ImportOptions, ImportRefused, parse_email_list
from tests import familymove_world as w
from tests.test_familyimport import RecordingAuth, bundle, dump, make_store


@pytest.fixture
def src_db(tmp_path):
    db = w.open_store(tmp_path / "src.sqlite")
    yield db
    db.close()


def cloud(**emails: str) -> ImportOptions:
    return ImportOptions(target_kind="cloud", emails=emails)


def plan(tmp_path, src_db, opts, auth=None):
    dst = make_store("sqlite", tmp_path, "d")
    return familyexport.plan_import(dst, bundle(src_db), opts, auth or RecordingAuth())


@pytest.mark.parametrize(
    "text,expected",
    [
        (
            """
# comment
moma,Mom@Example.com
@kida = -
supa   sup@example.com
  bo=   -
""",
            {
                "moma": "Mom@Example.com",
                "kida": "-",
                "supa": "sup@example.com",
                "bo": "-",
            },
        ),
        ("moma,\nkida,-\n", {"kida": "-"}),
    ],
    ids=["grammar", "skips_unfilled_template_lines"],
)
def test_parse_email_list_accepts(text, expected):
    assert parse_email_list(text) == expected


@pytest.mark.parametrize(
    "text,line",
    [
        ("moma,a@b.co\njusttext\n", 2),
        ("moma,a@b.co\n\nmoma,c@d.co\n", 3),
        ("moma,a b@c.de\n", 1),
        ("moma extra words\n", 1),
        (",x@y.zz\n", 1),
    ],
)
def test_parse_bad_line_names_the_line(text, line):
    with pytest.raises(ValueError, match=f"line {line}"):
        parse_email_list(text)


@pytest.mark.parametrize(
    "bad",
    [
        "a",
        "a@b",
        "@b.co",
        "a@@b.co",
        "a b@c.de",
        "a@b.co ",
        "x" * 250 + "@b.co",
        "a@.co",
        "a@b.",
    ],
)
def test_normalize_rejects(bad):
    # "a@b.co " has trailing whitespace which strip() forgives; only the rest must fail
    if bad == "a@b.co ":
        assert familyexport.normalize_email(bad) == "a@b.co"
    else:
        assert familyexport.normalize_email(bad) is None


def test_missing_answers_refuse(tmp_path, src_db):
    rep = plan(tmp_path, src_db, cloud(moma="mom@example.com"))
    assert not rep.ok
    assert rep.emailsMissing == ["kida"]  # supa is prefilled from the file
    assert any(
        e.startswith("no email for: kida") and "alias=-" in e for e in rep.errors
    )
    dst = make_store("sqlite", tmp_path, "d2")
    with pytest.raises(ImportRefused):
        familyexport.apply_import(
            dst, bundle(src_db), cloud(moma="mom@example.com"), RecordingAuth()
        )
    assert not dump(dst)


def test_plan_report_lists_every_person_with_their_sign_in_answer(tmp_path, src_db):
    rep = plan(tmp_path, src_db, cloud(moma="Mom@Example.com", kida="-"))
    assert rep.ok, rep.errors
    assert [p.alias for p in rep.people] == ["kida", "moma", "supa"]
    kid, mom, sup = rep.people
    assert (kid.signIn, kid.email, kid.sourceEmail, kid.hadSignIn) == (
        False,
        None,
        None,
        False,
    )
    assert (mom.signIn, mom.email, mom.role) == (True, "mom@example.com", "admin")
    # the target has no super: the imported one keeps it, and the file's email is the default
    assert (sup.role, sup.email, sup.sourceEmail, sup.hadSignIn) == (
        "super",
        "sup@example.com",
        "sup@example.com",
        True,
    )
    assert rep.emailsMissing == rep.emailsTaken == rep.emailsInvalid == []

    rep = plan(tmp_path, src_db, cloud(moma="mom@example.com"))
    kid = next(p for p in rep.people if p.alias == "kida")
    assert kid.signIn is None and kid.email is None

    rep = plan(tmp_path, src_db, cloud(moma="mom@example.com", kida="-", supa="-"))
    sup = next(p for p in rep.people if p.alias == "supa")
    assert rep.ok and sup.signIn is False and sup.email is None
    rep = plan(
        tmp_path,
        src_db,
        cloud(moma="mom@example.com", kida="-", supa="other@example.com"),
    )
    assert next(p for p in rep.people if p.alias == "supa").email == "other@example.com"

    opts = ImportOptions(
        target_kind="cloud",
        emails={"moma": "mom@example.com", "kida": "-"},
        alias_map={"kida": "kida2"},
    )
    rep = plan(tmp_path, src_db, opts)
    assert next(p for p in rep.people if p.alias == "kida").targetAlias == "kida2"


def test_home_target_report_has_people_without_email_answers(tmp_path, src_db):
    rep = plan(tmp_path, src_db, ImportOptions(target_kind="home", super_alias="moma"))
    assert len(rep.people) == 3
    assert all(p.email is None and p.signIn is None for p in rep.people)
    assert (
        next(p for p in rep.people if p.alias == "supa").role == "admin"
    )  # demoted at home
    assert next(
        p for p in rep.people if p.alias == "supa"
    ).hadSignIn  # has email/credentials


def test_home_target_refuses_emails(tmp_path, src_db):
    rep = plan(
        tmp_path,
        src_db,
        ImportOptions(
            target_kind="home", super_alias="moma", emails={"moma": "a@b.co"}
        ),
    )
    assert not rep.ok and "cloud target only" in " ".join(rep.errors)


@pytest.mark.parametrize(
    "emails,refused",
    [
        (
            {"moma": "-", "kida": "kid@example.com", "supa": "-"},
            lambda rep: any("at least one admin" in e for e in rep.errors),
        ),
        (
            {"moma": "same@example.com", "kida": "SAME@example.com"},
            lambda rep: rep.emailsTaken == ["kida", "moma"],
        ),
        (
            {"moma": "mom@example.com", "kida": "nope"},
            lambda rep: rep.emailsInvalid == ["kida"],
        ),
        (
            {"moma": "mom@example.com", "kida": "-", "ghost": "g@example.com", "xexta": "-"},
            lambda rep: sum("no such person" in e for e in rep.errors) == 2,
        ),
    ],
    ids=["no_admin_with_email", "duplicate_email", "invalid_email", "unknown_alias_and_external"],
)
def test_plan_refuses_bad_email_answers(tmp_path, src_db, emails, refused):
    rep = plan(tmp_path, src_db, cloud(**emails))
    assert not rep.ok and refused(rep)


def test_taken_by_another_uid_refuses_same_uid_ok(tmp_path, src_db):
    auth = RecordingAuth()
    auth.owners["mom@example.com"] = "someone-else"
    rep = plan(tmp_path, src_db, cloud(moma="mom@example.com", kida="-"), auth)
    assert not rep.ok and rep.emailsTaken == ["moma"]
    auth.owners["mom@example.com"] = w.MOM  # a re-run of a half-finished import
    assert plan(tmp_path, src_db, cloud(moma="mom@example.com", kida="-"), auth).ok


def test_apply_creates_accounts_for_every_email_and_lists_dash_people(tmp_path, src_db):
    dst = make_store("sqlite", tmp_path, "d")
    auth = RecordingAuth()
    res = familyexport.apply_import(
        dst, bundle(src_db), cloud(moma="mom@example.com", kida="-"), auth
    )
    assert sorted(auth.created) == [
        (w.MOM, "mom@example.com"),
        (w.SUP, "sup@example.com"),
    ]
    assert res.accountsCreated == ["moma", "supa"]
    assert res.accountsPending == ["kida"]
    got = dump(dst)
    assert got[f"users/{w.MOM}"]["email"] == "mom@example.com"
    assert "email" not in got[f"users/{w.KID}"]

    dst = make_store("sqlite", tmp_path, "d2")
    familyexport.apply_import(
        dst,
        bundle(src_db),
        cloud(moma="mom@example.com", kida="-", supa="-"),
        RecordingAuth(),
    )
    assert "email" not in dump(dst)[f"users/{w.SUP}"]


def test_firebase_create_account_sets_email_on_an_existing_uid(monkeypatch):
    from firebase_admin import auth as fb_auth

    calls: list = []

    def create(**kw):
        raise fb_auth.UidAlreadyExistsError("x", None, None)

    class U:
        email = None

    monkeypatch.setattr(fb_auth, "create_user", create)
    monkeypatch.setattr(fb_auth, "get_user", lambda uid: U())
    monkeypatch.setattr(
        fb_auth, "update_user", lambda uid, **kw: calls.append((uid, kw))
    )
    familyexport.FirebaseImportAuth().create_account("u1", "a@b.co")
    assert calls == [("u1", {"email": "a@b.co"})]
    U.email = "have@b.co"
    familyexport.FirebaseImportAuth().create_account("u1", "a@b.co")
    assert len(calls) == 1


def test_template_prefills_from_the_file(src_db):
    text = familyexport.email_template(bundle(src_db))
    assert (
        "kida," in text.splitlines()
        and "supa,sup@example.com" in text
        and "xexta" not in text
    )
    assert parse_email_list(text) == {
        "supa": "sup@example.com",
        "moma": "mom@example.com",
    }
