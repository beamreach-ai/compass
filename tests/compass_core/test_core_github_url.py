"""GITHUB_API_URL: where every GitHub call goes (github.com, GitHub Enterprise Server, the e2e fake)."""

from __future__ import annotations

from captains.prs.github_pr_client import graphql_url
from compass_core import github


def test_github_com_by_default_and_the_setting_when_given(monkeypatch):
    monkeypatch.delenv("GITHUB_API_URL", raising=False)
    assert github.api_url() == "https://api.github.com" and graphql_url() == "https://api.github.com/graphql"
    monkeypatch.setenv("GITHUB_API_URL", "https://ghe.example.com/api/v3/")
    assert github.api_url() == "https://ghe.example.com/api/v3"
    assert graphql_url() == "https://ghe.example.com/api/graphql", "Enterprise Server serves GraphQL beside /api/v3"
    monkeypatch.setenv("GITHUB_API_URL", "http://fakes:9000/github")
    assert graphql_url() == "http://fakes:9000/github/graphql"


def test_the_client_talks_to_the_configured_api(monkeypatch):
    monkeypatch.setenv("GITHUB_API_URL", "https://ghe.example.com/api/v3")
    client = github.client("t0ken")
    assert client.requester.base_url == "https://ghe.example.com/api/v3"


def test_the_clone_url_follows_the_api_unless_set(monkeypatch):
    monkeypatch.delenv("GITHUB_GIT_URL", raising=False)
    monkeypatch.delenv("GITHUB_API_URL", raising=False)
    assert github.git_url("acme/infra") == "https://github.com/acme/infra.git"
    assert github.git_url("acme/infra", "t0k") == "https://x-access-token:t0k@github.com/acme/infra.git"
    monkeypatch.setenv("GITHUB_API_URL", "https://ghe.example.com/api/v3")
    assert github.git_url("acme/infra") == "https://ghe.example.com/acme/infra.git"
    monkeypatch.setenv("GITHUB_GIT_URL", "http://fakes:9000/git/")
    assert github.git_url("acme/infra", "t0k") == "http://x-access-token:t0k@fakes:9000/git/acme/infra.git"


def test_pull_requests_on_any_github_host_are_tracked():
    from compass_core.registry import parse_pr_url

    assert parse_pr_url("https://github.com/acme/infra/pull/7") == ("acme/infra", 7)
    assert parse_pr_url("https://ghe.example.com/acme/infra/pull/12") == ("acme/infra", 12)
    assert parse_pr_url("not a url") is None
