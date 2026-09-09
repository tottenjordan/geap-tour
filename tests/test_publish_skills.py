"""Tests for the Skill Registry publish/inspect CLI (src/skills/publish_skills.py).

The hard part of testing a registry CLI is that a fake client which cheerfully
answers every method passes whether or not the SDK is being called correctly —
the failure mode docs/notes/checks-that-cannot-detect-their-own-failure.md is
about. So `_FakeSkillsApi` below is not a MagicMock: it binds every call
against the REAL `agentplatform._genai.skills.Skills` signature, validates
every `config` dict against the REAL config model's field names, checks the
`local_path` directory really exists (which is only true if the call happens
inside `materialize_skill`'s `with`), and stores state so idempotency is a
behavioural property rather than an assertion about call counts.

The response side gets the same treatment in `TestSdkResponseFields`: the fake
can only be trusted to stand in for a real response if the field names it hands
back are the real ones.
"""

import inspect
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

import src.skills.publish_skills as publisher
from src.skills import SKILL_DEFINITIONS


def _real_skills_signature(method: str) -> inspect.Signature:
    """The installed SDK's signature for `client.skills.<method>`, minus `self`.

    Binding our real call kwargs against this is what makes these tests survive
    the SDK renaming or reordering a parameter: a rename turns into a TypeError
    here instead of a green run against a fake that never cared.
    """
    from agentplatform._genai.skills import Skills

    sig = inspect.signature(getattr(Skills, method))
    return sig.replace(parameters=[p for p in sig.parameters.values() if p.name != "self"])


def _config_field_names(model_name: str) -> set[str]:
    """Field names of a real `agentplatform` skills config model.

    Read off `model_fields` rather than relying on the models' own
    `extra="forbid"`: `src/eval/_sdk_patches._flip_extra_to_ignore()` flips every
    `agentplatform._genai.types` model to `extra="ignore"` process-wide, so if an
    eval test ran first, `model_validate` would silently accept a typo'd key.
    """
    from agentplatform._genai.types import common

    return set(getattr(common, model_name).model_fields)


def _api_error(code: int, status: str, message: str):
    """A `google.genai` APIError shaped the way the transport really builds one."""
    from google.genai import errors

    cls = errors.ClientError if code < 500 else errors.ServerError
    return cls(code, {"error": {"code": code, "status": status, "message": message}})


class _FakeSkillsApi:
    """Stateful stand-in for `client.skills`, validated against the real SDK."""

    def __init__(self) -> None:
        self.stored: dict[str, SimpleNamespace] = {}
        self.calls: list[tuple[str, dict]] = []
        self.payloads: dict[str, str] = {}  # skill_id -> SKILL.md as uploaded
        self.raise_on: dict[str, BaseException] = {}  # method -> error to raise
        self.raise_for_skill: dict[str, BaseException] = {}  # skill_id -> error

    # -- helpers ---------------------------------------------------------
    def _record(self, method: str, kwargs: dict, config_model: str | None = None) -> None:
        _real_skills_signature(method).bind(**kwargs)
        config = kwargs.get("config")
        if config is not None:
            assert config_model is not None, f"{method} was not expected to take a config"
            unknown = set(config) - _config_field_names(config_model)
            assert not unknown, f"{method}(config=...) has keys {sorted(unknown)} the SDK rejects"
        self.calls.append((method, kwargs))
        if method in self.raise_on:
            raise self.raise_on[method]

    def _ingest(self, skill_id: str, local_path: str) -> None:
        directory = Path(local_path)
        assert directory.is_dir(), (
            f"local_path {local_path} does not exist at call time — the create/update call "
            "must happen inside materialize_skill's `with` block"
        )
        assert [p.name for p in directory.iterdir()] == ["SKILL.md"]
        self.payloads[skill_id] = (directory / "SKILL.md").read_text(encoding="utf-8")

    @staticmethod
    def _skill_id_of(name: str) -> str:
        return name.rsplit("/", 1)[-1]

    # -- the SDK surface -------------------------------------------------
    def create(self, **kwargs):
        self._record("create", kwargs, "CreateSkillConfig")
        skill_id = kwargs["skill_id"]
        if skill_id in self.raise_for_skill:
            raise self.raise_for_skill[skill_id]
        name = publisher.skill_resource_name(skill_id)
        if name in self.stored:
            # AIP-standard behaviour for a create against an existing id. This is
            # what makes "publish twice" a red test if the lookup is removed.
            raise _api_error(409, "ALREADY_EXISTS", f"Skill {name} already exists")
        self._ingest(skill_id, kwargs["config"]["local_path"])
        self.stored[name] = SimpleNamespace(
            name=name,
            display_name=kwargs["display_name"],
            description=kwargs["description"],
        )
        return self.stored[name]

    def get(self, **kwargs):
        self._record("get", kwargs)
        name = kwargs["name"]
        if name not in self.stored:
            raise _api_error(404, "NOT_FOUND", f"Skill {name} not found")
        return self.stored[name]

    def update(self, **kwargs):
        self._record("update", kwargs, "UpdateSkillConfig")
        name = kwargs["name"]
        skill_id = self._skill_id_of(name)
        if skill_id in self.raise_for_skill:
            raise self.raise_for_skill[skill_id]
        if name not in self.stored:
            raise _api_error(404, "NOT_FOUND", f"Skill {name} not found")
        config = kwargs["config"]
        if config.get("local_path"):
            self._ingest(skill_id, config["local_path"])
        stored = self.stored[name]
        stored.display_name = config.get("display_name", stored.display_name)
        stored.description = config.get("description", stored.description)
        return stored

    def delete(self, **kwargs):
        self._record("delete", kwargs)
        name = kwargs["name"]
        if name not in self.stored:
            raise _api_error(404, "NOT_FOUND", f"Skill {name} not found")
        del self.stored[name]
        return SimpleNamespace(name=name, done=True)

    def list(self, **kwargs):
        self._record("list", kwargs, "ListSkillsConfig")
        return iter(list(self.stored.values()))

    def retrieve(self, **kwargs):
        self._record("retrieve", kwargs, "RetrieveSkillsConfig")
        query = kwargs["query"].lower()
        hits = [
            SimpleNamespace(skill_name=s.name, description=s.description)
            for s in self.stored.values()
            if any(word in s.description.lower() for word in query.split())
        ]
        top_k = (kwargs.get("config") or {}).get("top_k")
        return SimpleNamespace(retrieved_skills=hits[:top_k] if top_k else hits)


class _FakeClient:
    def __init__(self, skills: _FakeSkillsApi | None = None) -> None:
        self.skills = skills or _FakeSkillsApi()


@pytest.fixture
def api() -> _FakeSkillsApi:
    return _FakeSkillsApi()


@pytest.fixture
def client(api: _FakeSkillsApi) -> _FakeClient:
    return _FakeClient(api)


class TestSkillResourceName:
    def test_bare_id_becomes_a_full_resource_path(self):
        from src.config import GCP_PROJECT_ID, GCP_REGION

        assert publisher.skill_resource_name("receipt-audit") == (
            f"projects/{GCP_PROJECT_ID}/locations/{GCP_REGION}/skills/receipt-audit"
        )

    def test_a_full_resource_name_passes_through(self):
        name = "projects/other/locations/europe-west4/skills/receipt-audit"
        assert publisher.skill_resource_name(name) == name

    def test_collection_relative_name_is_normalised(self):
        # `client.skills.list()` items and a hand-typed `skills/<id>` both have to
        # round-trip to the same absolute name, or update/delete target nothing.
        assert publisher.skill_resource_name("skills/receipt-audit") == (
            publisher.skill_resource_name("receipt-audit")
        )


class TestPublish:
    def test_publishes_every_skill(self, client, api):
        results = publisher.publish_skills(client=client)

        assert [r.action for r in results] == [publisher.ACTION_CREATED] * len(SKILL_DEFINITIONS)
        assert len(api.stored) == len(SKILL_DEFINITIONS)
        for definition in SKILL_DEFINITIONS:
            stored = api.stored[publisher.skill_resource_name(definition.skill_id)]
            assert stored.display_name == definition.display_name
            assert stored.description == definition.description
            # The uploaded bytes are the in-repo SKILL.md, not a re-derived copy.
            assert api.payloads[definition.skill_id] == definition.skill_md

    def test_create_kwargs_are_exactly_the_sdk_contract(self, client, api):
        publisher.publish_skills(client=client)
        _, kwargs = next((m, k) for m, k in api.calls if m == "create")
        # Every REQUIRED parameter of the real signature is supplied by name.
        required = {
            name
            for name, p in _real_skills_signature("create").parameters.items()
            if p.default is inspect.Parameter.empty
        }
        assert required == {"skill_id", "display_name", "description"}
        assert required <= set(kwargs)
        assert set(kwargs["config"]) == {"local_path"}

    def test_republishing_updates_in_place(self, client, api):
        """Red-state proof for idempotency.

        The fake raises ALREADY_EXISTS on a create against a stored id, exactly as
        the API does. Delete the get-then-update branch in `_publish_one` and this
        test goes from all-updated to all-failed.
        """
        publisher.publish_skills(client=client)
        api.calls.clear()

        results = publisher.publish_skills(client=client)

        # The property that matters: one registry entry per skill, not two.
        assert len(api.stored) == len(SKILL_DEFINITIONS)
        assert [r.action for r in results] == [publisher.ACTION_UPDATED] * len(SKILL_DEFINITIONS)
        second = [method for method, _ in api.calls]
        assert "create" not in second, "a re-publish must not re-create an existing skill"
        assert second.count("update") == len(SKILL_DEFINITIONS)
        assert second[0] == "get", "the existence probe must come before the write"

    def test_update_reuploads_the_current_skill_md(self, client, api):
        publisher.publish_skills(client=client)
        api.payloads.clear()
        publisher.publish_skills(client=client)
        # An update that forgot local_path would leave the registry serving the
        # first revision's instructions forever.
        assert api.payloads == {d.skill_id: d.skill_md for d in SKILL_DEFINITIONS}

    def test_dry_run_touches_nothing(self, client, api):
        results = publisher.publish_skills(client=client, dry_run=True)
        assert [r.action for r in results] == [publisher.ACTION_DRY_RUN] * len(SKILL_DEFINITIONS)
        assert api.calls == []
        assert api.stored == {}

    def test_dry_run_does_not_even_construct_a_client(self, monkeypatch):
        # "Makes no calls" has to include the client itself: constructing it runs
        # ADC discovery and, on a GCE/Cloud Run host, hits the metadata server.
        def _boom():
            raise AssertionError("--dry-run must not touch credentials or the network")

        monkeypatch.setattr(publisher, "build_client", _boom)
        assert publisher.main(["--dry-run"]) == 0
        assert publisher.main(["--delete", "receipt-audit", "--dry-run"]) == 0

    def test_publishes_a_caller_supplied_subset(self, client, api):
        only = (SKILL_DEFINITIONS[0],)
        results = publisher.publish_skills(only, client=client)
        assert [r.skill_id for r in results] == [only[0].skill_id]
        assert len(api.stored) == 1


class TestFailurePosture:
    """Outcome 1 (preview absent) vs outcome 2 (a real failure) vs outcome 3."""

    def test_outcome_3_success_exits_zero_and_reports_n_of_m(self, client, monkeypatch, caplog):
        monkeypatch.setattr(publisher, "build_client", lambda: client)
        with caplog.at_level(logging.INFO):
            rc = publisher.main([])
        assert rc == 0
        assert f"{len(SKILL_DEFINITIONS)}/{len(SKILL_DEFINITIONS)}" in caplog.text

    def test_outcome_1_missing_collection_is_a_skip(self, client, api, monkeypatch, caplog):
        # The skills surface is not served in this project/region, so *every*
        # method 404s — including the `list` probe that would otherwise prove the
        # collection alive.
        api.raise_on["create"] = _api_error(404, "NOT_FOUND", "Method not found.")
        api.raise_on["list"] = _api_error(404, "NOT_FOUND", "Method not found.")
        results = publisher.publish_skills(client=client)
        assert [r.action for r in results] == [publisher.ACTION_SKIPPED] * len(SKILL_DEFINITIONS)

        monkeypatch.setattr(publisher, "build_client", lambda: client)
        with caplog.at_level(logging.INFO):
            assert publisher.main([]) == 0
        assert publisher.SKILL_REGISTRY_SKIP in caplog.text

    def test_outcome_1_unimplemented_501_is_a_skip(self, client, api):
        # The other "endpoint isn't served" code. Unlike 404 it is unambiguous,
        # so it needs no collection probe.
        api.raise_on["create"] = _api_error(501, "UNIMPLEMENTED", "Method not implemented.")
        results = publisher.publish_skills(client=client)
        assert all(r.action == publisher.ACTION_SKIPPED for r in results)

    def test_outcome_1_skip_message_names_the_project_and_location(
        self, client, api, monkeypatch, caplog
    ):
        # "preview not enabled" is unactionable without knowing what it addressed
        # — a mis-set GCP_REGION produces exactly this line.
        from src.config import GCP_PROJECT_ID, GCP_REGION

        api.raise_on["create"] = _api_error(501, "UNIMPLEMENTED", "Method not implemented.")
        monkeypatch.setattr(publisher, "build_client", lambda: client)
        with caplog.at_level(logging.INFO):
            assert publisher.main([]) == 0
        assert f"project={GCP_PROJECT_ID}" in caplog.text
        assert f"location={GCP_REGION}" in caplog.text

    def test_outcome_2_a_404_on_update_is_a_failure_not_a_skip(
        self, client, api, monkeypatch, caplog
    ):
        """A resource-level 404 must never read as "the surface is absent".

        `get` answered with the skill, so the registry is demonstrably here; a
        404 from the `update` that follows means the skill was deleted between
        the two calls. Routing that through the unavailability classifier
        reported a genuine failure as a skip and exited 0 — the exact collapse
        this module exists to avoid.
        """
        publisher.publish_skills(client=client)
        api.raise_on["update"] = _api_error(404, "NOT_FOUND", "Skill not found")
        monkeypatch.setattr(publisher, "build_client", lambda: client)

        results = publisher.publish_skills(client=client)
        assert [r.action for r in results] == [publisher.ACTION_FAILED] * len(SKILL_DEFINITIONS)

        with caplog.at_level(logging.INFO):
            assert publisher.main([]) == 1
        assert publisher.SKILL_REGISTRY_SKIP not in caplog.text

    def test_outcome_2_a_404_on_create_against_a_live_collection_is_a_failure(
        self, client, api, monkeypatch, caplog
    ):
        # A create-only 404 (e.g. a parent that does not exist) while `list`
        # still answers: the endpoint IS served here, so our call was refused.
        api.raise_on["create"] = _api_error(404, "NOT_FOUND", "parent not found")
        monkeypatch.setattr(publisher, "build_client", lambda: client)

        results = publisher.publish_skills(client=client)
        assert [r.action for r in results] == [publisher.ACTION_FAILED] * len(SKILL_DEFINITIONS)

        with caplog.at_level(logging.INFO):
            assert publisher.main([]) == 1
        assert publisher.SKILL_REGISTRY_SKIP not in caplog.text

    def test_outcome_2_an_unanticipated_exception_type_is_a_failure(
        self, client, api, monkeypatch, caplog
    ):
        """The classifier's fallthrough: unrecognised means failure, not skip.

        Nothing else pins this. Widening the last branch to `return True`, or
        wrapping the classifier in a bare `except`, would keep every other test
        in this class green while turning a live 500 into exit 0.
        """
        api.raise_on["create"] = RuntimeError("connection reset by peer")
        monkeypatch.setattr(publisher, "build_client", lambda: client)

        results = publisher.publish_skills(client=client)
        assert [r.action for r in results] == [publisher.ACTION_FAILED] * len(SKILL_DEFINITIONS)

        with caplog.at_level(logging.INFO):
            assert publisher.main([]) == 1
        assert publisher.SKILL_REGISTRY_SKIP not in caplog.text

    def test_outcome_1_service_disabled_403_is_a_skip(self, client, api):
        api.raise_on["create"] = _api_error(
            403,
            "PERMISSION_DENIED",
            "Vertex AI API has not been used in project 1234 before or it is disabled.",
        )
        results = publisher.publish_skills(client=client)
        assert all(r.action == publisher.ACTION_SKIPPED for r in results)

    def test_outcome_1_sdk_without_a_skills_surface_is_a_skip(self, monkeypatch, caplog):
        monkeypatch.setattr(publisher, "build_client", lambda: SimpleNamespace())
        with caplog.at_level(logging.INFO):
            assert publisher.main([]) == 0
        assert publisher.SKILL_REGISTRY_SKIP in caplog.text

    def test_outcome_2_bad_payload_exits_non_zero(self, client, api, monkeypatch, caplog):
        api.raise_on["create"] = _api_error(400, "INVALID_ARGUMENT", "description is required")
        results = publisher.publish_skills(client=client)
        assert [r.action for r in results] == [publisher.ACTION_FAILED] * len(SKILL_DEFINITIONS)

        monkeypatch.setattr(publisher, "build_client", lambda: client)
        with caplog.at_level(logging.INFO):
            assert publisher.main([]) == 1
        assert publisher.SKILL_REGISTRY_SKIP not in caplog.text

    def test_outcome_2_plain_permission_denied_is_a_failure_not_a_skip(self, client, api):
        # An IAM denial means the surface exists and refused *us*. Reading that as
        # "preview not enabled" is precisely the collapse this module must avoid.
        api.raise_on["create"] = _api_error(
            403, "PERMISSION_DENIED", "Permission 'aiplatform.skills.create' denied on resource"
        )
        results = publisher.publish_skills(client=client)
        assert all(r.action == publisher.ACTION_FAILED for r in results)

    def test_outcome_2_quota_and_server_errors_are_failures(self, client, api):
        for error in (
            _api_error(429, "RESOURCE_EXHAUSTED", "Quota exceeded"),
            _api_error(503, "UNAVAILABLE", "backend unavailable"),
        ):
            api.raise_on["create"] = error
            results = publisher.publish_skills(client=client)
            assert all(r.action == publisher.ACTION_FAILED for r in results), error

    def test_a_partial_failure_still_publishes_the_rest_and_exits_non_zero(
        self, client, api, monkeypatch, caplog
    ):
        broken = SKILL_DEFINITIONS[1].skill_id
        api.raise_for_skill[broken] = _api_error(400, "INVALID_ARGUMENT", "nope")
        monkeypatch.setattr(publisher, "build_client", lambda: client)

        with caplog.at_level(logging.INFO):
            rc = publisher.main([])

        assert rc == 1
        assert len(api.stored) == len(SKILL_DEFINITIONS) - 1
        assert f"{len(SKILL_DEFINITIONS) - 1}/{len(SKILL_DEFINITIONS)}" in caplog.text
        assert broken in caplog.text

    def test_an_update_failure_is_reported(self, client, api):
        publisher.publish_skills(client=client)
        api.raise_on["update"] = _api_error(400, "INVALID_ARGUMENT", "bad update mask")
        results = publisher.publish_skills(client=client)
        assert all(r.action == publisher.ACTION_FAILED for r in results)

    def test_a_403_on_the_existence_probe_is_a_failure(self, client, api):
        # `get` 404 means "no such skill" and must stay a normal create; anything
        # else on the probe is a real failure and must not be swallowed.
        api.raise_on["get"] = _api_error(403, "PERMISSION_DENIED", "denied on resource")
        results = publisher.publish_skills(client=client)
        assert all(r.action == publisher.ACTION_FAILED for r in results)


class TestListSearchDelete:
    def test_list_returns_the_registered_skills(self, client, api):
        publisher.publish_skills(client=client)
        listed = publisher.list_skills(client=client)
        assert {s.name for s in listed} == set(api.stored)

    def test_list_cli_prints_names_and_exits_zero(self, client, monkeypatch, capsys):
        publisher.publish_skills(client=client)
        monkeypatch.setattr(publisher, "build_client", lambda: client)
        assert publisher.main(["--list"]) == 0
        out = capsys.readouterr().out
        for definition in SKILL_DEFINITIONS:
            assert definition.skill_id in out

    def test_list_degrades_when_the_surface_is_absent(self, client, api, monkeypatch, caplog):
        api.raise_on["list"] = _api_error(404, "NOT_FOUND", "Method not found.")
        monkeypatch.setattr(publisher, "build_client", lambda: client)
        with caplog.at_level(logging.INFO):
            assert publisher.main(["--list"]) == 0
        assert publisher.SKILL_REGISTRY_SKIP in caplog.text

    def test_list_reports_a_real_failure(self, client, api, monkeypatch):
        api.raise_on["list"] = _api_error(403, "PERMISSION_DENIED", "denied on resource")
        monkeypatch.setattr(publisher, "build_client", lambda: client)
        assert publisher.main(["--list"]) == 1

    def test_search_uses_the_semantic_retrieve_surface(self, client, api):
        # `retrieve`, not a client-side filter over `list`: the point of --search
        # is to exercise the same semantic index the agent hits at runtime.
        publisher.publish_skills(client=client)
        publisher.search_skills("receipt", client=client)
        assert [method for method, _ in api.calls if method == "retrieve"] == ["retrieve"]

    def test_search_bounds_the_request_through_the_real_config_field(self, client, api):
        publisher.publish_skills(client=client)
        publisher.search_skills("expense receipt booking", client=client)
        _, kwargs = next((m, k) for m, k in api.calls if m == "retrieve")
        assert kwargs["query"] == "expense receipt booking"
        # Pins the SDK's real RetrieveSkillsConfig field name; the fake rejects
        # any key the model does not declare.
        assert kwargs["config"] == {"top_k": 5}

    def test_search_cli_exits_zero_when_nothing_matches(self, client, monkeypatch, caplog):
        publisher.publish_skills(client=client)
        monkeypatch.setattr(publisher, "build_client", lambda: client)
        with caplog.at_level(logging.INFO):
            assert publisher.main(["--search", "zzzz-nothing-matches"]) == 0

    def test_search_degrades_when_the_surface_is_absent(self, client, api, monkeypatch, caplog):
        api.raise_on["retrieve"] = _api_error(404, "NOT_FOUND", "Method not found.")
        monkeypatch.setattr(publisher, "build_client", lambda: client)
        with caplog.at_level(logging.INFO):
            assert publisher.main(["--search", "receipt"]) == 0
        assert publisher.SKILL_REGISTRY_SKIP in caplog.text

    def test_delete_removes_the_skill(self, client, api, monkeypatch):
        publisher.publish_skills(client=client)
        target = SKILL_DEFINITIONS[0].skill_id
        monkeypatch.setattr(publisher, "build_client", lambda: client)

        assert publisher.main(["--delete", target]) == 0

        assert publisher.skill_resource_name(target) not in api.stored
        assert len(api.stored) == len(SKILL_DEFINITIONS) - 1
        _, kwargs = next((m, k) for m, k in api.calls if m == "delete")
        assert kwargs["name"] == publisher.skill_resource_name(target)

    def test_delete_dry_run_touches_nothing(self, client, api, monkeypatch):
        publisher.publish_skills(client=client)
        api.calls.clear()
        monkeypatch.setattr(publisher, "build_client", lambda: client)

        assert publisher.main(["--delete", SKILL_DEFINITIONS[0].skill_id, "--dry-run"]) == 0

        assert len(api.stored) == len(SKILL_DEFINITIONS)
        assert "delete" not in [method for method, _ in api.calls]

    def test_deleting_an_unknown_skill_is_a_failure_not_a_skip(self, client, monkeypatch, caplog):
        # A 404 here is about the *id*, not the API: the collection probe proves
        # the surface is alive, so this must read red rather than "preview off".
        monkeypatch.setattr(publisher, "build_client", lambda: client)
        with caplog.at_level(logging.INFO):
            assert publisher.main(["--delete", "no-such-skill"]) == 1
        assert publisher.SKILL_REGISTRY_SKIP not in caplog.text

    def test_delete_degrades_when_the_surface_is_absent(self, client, api, monkeypatch, caplog):
        api.raise_on["get"] = _api_error(404, "NOT_FOUND", "Method not found.")
        api.raise_on["list"] = _api_error(404, "NOT_FOUND", "Method not found.")
        monkeypatch.setattr(publisher, "build_client", lambda: client)
        with caplog.at_level(logging.INFO):
            assert publisher.main(["--delete", "receipt-audit"]) == 0
        assert publisher.SKILL_REGISTRY_SKIP in caplog.text


class TestClientConstruction:
    def test_builds_an_agentplatform_client_not_a_vertexai_one(self, monkeypatch):
        # The two are separate module copies; `vertexai.Client` also FutureWarns.
        # See docs/notes/agentplatform-client-migration.md.
        captured = {}

        def _fake_client(**kwargs):
            captured.update(kwargs)
            return SimpleNamespace(skills=object())

        import agentplatform

        monkeypatch.setattr(agentplatform, "Client", _fake_client)
        publisher.build_client()

        from src.config import GCP_PROJECT_ID, GCP_REGION

        assert captured == {"project": GCP_PROJECT_ID, "location": GCP_REGION}

    def test_missing_credentials_degrade_to_a_skip(self, monkeypatch, caplog):
        from google.auth import exceptions as auth_exceptions

        def _no_adc():
            raise auth_exceptions.DefaultCredentialsError("no ADC")

        monkeypatch.setattr(publisher, "build_client", _no_adc)
        with caplog.at_level(logging.INFO):
            assert publisher.main([]) == 0
        assert publisher.SKILL_REGISTRY_SKIP in caplog.text


class TestSdkResponseFields:
    """Pin the *response* field names the CLI reads off the SDK.

    `_config_field_names` already pins the request side. Nothing pinned this
    side, and it is the more dangerous one: reading a renamed field through a
    `getattr(..., default)` does not fail, it answers. A renamed
    `retrieved_skills` made `--search` print "0 match(es)" and exit 0 — a
    confident *wrong* answer to the only question the subcommand asks. These
    assertions are read off the installed models, so a rename turns into a red
    test here instead of a silent lie in the field.
    """

    @staticmethod
    def _model(name: str):
        from agentplatform._genai.types import common

        return getattr(common, name)

    def test_retrieve_response_fields_are_the_ones_search_reads(self):
        assert "retrieved_skills" in self._model("RetrieveSkillsResponse").model_fields
        assert {"skill_name", "description"} <= set(self._model("RetrievedSkill").model_fields)

    def test_skill_fields_are_the_ones_list_prints(self):
        assert {"name", "display_name"} <= set(self._model("Skill").model_fields)

    def test_a_renamed_response_field_is_loud_not_zero_matches(self, client, api, monkeypatch):
        # The behavioural half: with a `getattr(..., None) or []` this exits 0
        # reporting no matches. Plain attribute access makes it a red exit.
        api.retrieve = lambda **kwargs: SimpleNamespace(retrievedSkills=[])
        monkeypatch.setattr(publisher, "build_client", lambda: client)
        assert publisher.main(["--search", "receipt"]) == 1


class TestResourceNameOf:
    """`_resource_name_of` exists for one case the happy path never produces."""

    def test_prefers_the_servers_own_skill_name(self):
        served = SimpleNamespace(name="projects/p/locations/l/skills/receipt-audit")
        assert publisher._resource_name_of(served, "fallback") == served.name

    def test_an_operation_name_is_rejected_in_favour_of_the_addressed_skill(self):
        """The reason the `/skills/` filter is there.

        `create`/`update` return a `SkillOperation` (a real model in this SDK —
        `agentplatform._genai.types.common.SkillOperation`) rather than a `Skill`
        whenever the call does not wait for completion, and an operation's
        `name` is the operation's own. Reporting it as the skill's resource name
        would print, and store, something that addresses nothing.
        """
        operation = SimpleNamespace(name="projects/p/locations/l/operations/12345")
        addressed = publisher.skill_resource_name("receipt-audit")
        assert publisher._resource_name_of(operation, addressed) == addressed

    def test_a_missing_or_non_string_name_falls_back(self):
        assert publisher._resource_name_of(SimpleNamespace(), "fallback") == "fallback"
        assert publisher._resource_name_of(SimpleNamespace(name=None), "fallback") == "fallback"
