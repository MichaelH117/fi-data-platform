"""Brand scrub: the public repo holds no forbidden term (list is private, so the repo test skips without it)."""

from __future__ import annotations

from pathlib import Path

import pytest

from tools import brand_scrub as bs


def test_matcher_rules(tmp_path: Path) -> None:
    terms_file = tmp_path / "terms.txt"
    terms_file.write_text("# comment\nAcme Corp\ncs:ZZQ\nA&B Co\n", encoding="utf-8")
    terms = bs.load_terms(terms_file)
    repo = tmp_path / "repo"
    (repo / "docs").mkdir(parents=True)
    (repo / "_private").mkdir()
    (repo / "site").mkdir()
    (repo / "docs" / "a.md").write_text("ok line\nwe met ACME corp today\nzzq is fine\nZZQ is not\n", encoding="utf-8")
    (repo / "docs" / "b.md").write_text("acmecorporation and xZZQx are not words\nA&B Co here\n", encoding="utf-8")
    (repo / "_private" / "notes.md").write_text("Acme Corp\n", encoding="utf-8")
    (repo / "site" / "page.html").write_text("Acme Corp\n", encoding="utf-8")

    hits = {str(h) for h in bs.scan(repo, terms)}
    assert hits == {"docs/a.md:2: Acme Corp", "docs/a.md:4: ZZQ", "docs/b.md:2: A&B Co"}
    assert "site/page.html:1: Acme Corp" in {str(h) for h in bs.scan(repo, terms, include_generated=True)}


def test_missing_term_list_exits_2(tmp_path: Path) -> None:
    assert bs.main(["--terms", str(tmp_path / "absent.txt"), "--root", str(tmp_path)]) == 2


@pytest.mark.skipif(not bs.DEFAULT_TERMS.exists(), reason="private forbidden-term list not present")
def test_repo_is_brand_free() -> None:
    hits = bs.scan(bs.REPO_ROOT, bs.load_terms(bs.DEFAULT_TERMS))
    assert not hits, "forbidden terms found:\n" + "\n".join(map(str, hits[:50]))
