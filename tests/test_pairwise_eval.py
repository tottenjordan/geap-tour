"""Pairwise side-by-side (SxS) win-rate eval for the coordinator bake-off.

Google's guidance for A-vs-B is a pairwise autorater (judge picks a winner)
rather than diffing pointwise scores. The installed vertexai SDK exposes no
clean ``PairwiseMetric``/``AutoraterConfig`` class (same SDK-version reality that
forced ``policy_judge`` off ``client.evals``), so the shipping path is a
standalone ``google.genai`` judge that emits ``Choice: A|B|TIE``, with
``flip_enabled`` (position-bias cancellation) and ``sampling_count`` (majority
vote) implemented for real. All model/network calls are faked here.
"""

import pandas as pd
import pytest

from src.eval import pairwise_eval as pw


class TestParseChoice:
    def test_parses_final_choice_line(self):
        assert pw.parse_ab_choice("Reasoning...\nChoice: A") == "A"
        assert pw.parse_ab_choice("Choice: B") == "B"
        assert pw.parse_ab_choice("Choice: TIE") == "TIE"

    def test_uses_last_match(self):
        # Judges often restate the options before the verdict.
        assert pw.parse_ab_choice("A is good, B is better. Choice: B") == "B"

    def test_case_insensitive(self):
        assert pw.parse_ab_choice("choice: tie") == "TIE"

    def test_none_when_absent(self):
        assert pw.parse_ab_choice("no verdict here") is None
        assert pw.parse_ab_choice("") is None
        assert pw.parse_ab_choice(None) is None


class TestConfig:
    def test_defaults_flip_and_sampling(self):
        cfg = pw.PairwiseConfig()
        assert cfg.flip_enabled is True
        assert cfg.sampling_count == 4
        assert cfg.judge_model  # some default judge


class TestAggregate:
    def test_win_tie_rates(self):
        choices = [pw.CANDIDATE, pw.CANDIDATE, pw.BASELINE, pw.TIE]
        agg = pw.aggregate_choices(choices)
        assert agg["n_cases"] == 4
        assert agg["win_rate_candidate"] == pytest.approx(0.5)
        assert agg["win_rate_baseline"] == pytest.approx(0.25)
        assert agg["tie_rate"] == pytest.approx(0.25)

    def test_empty_is_zero(self):
        agg = pw.aggregate_choices([])
        assert agg["n_cases"] == 0
        assert agg["win_rate_candidate"] == 0.0
        assert agg["win_rate_baseline"] == 0.0
        assert agg["tie_rate"] == 0.0

    def test_significance_block_present_and_ties_excluded(self):
        # 8 candidate, 2 baseline, 3 ties -> decisive denominator is 10
        choices = [pw.CANDIDATE] * 8 + [pw.BASELINE] * 2 + [pw.TIE] * 3
        agg = pw.aggregate_choices(choices)
        sig = agg["significance"]
        assert sig["decisive"] == 10
        assert sig["win_rate_decisive"] == pytest.approx(0.8)
        assert 0.0 <= sig["p_value"] <= 1.0

    def test_decisive_sweep_is_significant(self):
        agg = pw.aggregate_choices([pw.CANDIDATE] * 10)
        assert agg["significance"]["significant"] is True
        assert agg["significance"]["p_value"] < 0.05

    def test_coin_flip_not_significant(self):
        agg = pw.aggregate_choices([pw.CANDIDATE] * 5 + [pw.BASELINE] * 5)
        assert agg["significance"]["significant"] is False

    def test_empty_significance_is_safe(self):
        sig = pw.aggregate_choices([])["significance"]
        assert sig["decisive"] == 0
        assert sig["significant"] is False


class TestPairedDataset:
    def test_carries_both_response_columns(self):
        df = pw.build_paired_dataset(["p1", "p2"], ["b1", "b2"], ["c1", "c2"])
        assert list(df.columns) == ["prompt", "baseline_response", "candidate_response"]
        assert df.iloc[0]["baseline_response"] == "b1"
        assert df.iloc[0]["candidate_response"] == "c1"


class TestFlipDebias:
    def test_flip_swaps_position_and_inverts_result(self):
        # A judge that ALWAYS picks "A" (pure position bias toward whoever is
        # shown first) must net to a TIE once flip cancels the bias: on the
        # unflipped sample A=baseline (baseline "wins"), on the flipped sample
        # A=candidate (candidate "wins") -> 1 vote each -> TIE.
        seen = []

        def always_a(prompt):
            seen.append(prompt)
            return "Choice: A"

        cfg = pw.PairwiseConfig(sampling_count=2, flip_enabled=True)
        choice = pw.judge_case("prompt", "BASE_RESP", "CAND_RESP", always_a, cfg)
        assert choice == pw.TIE
        # Second sample presented candidate BEFORE baseline (position swapped).
        assert "CAND_RESP" in seen[1]
        assert seen[1].index("CAND_RESP") < seen[1].index("BASE_RESP")

    def test_no_flip_keeps_order(self):
        seen = []

        def always_a(prompt):
            seen.append(prompt)
            return "Choice: A"

        cfg = pw.PairwiseConfig(sampling_count=2, flip_enabled=False)
        choice = pw.judge_case("prompt", "BASE_RESP", "CAND_RESP", always_a, cfg)
        # Without flip, position bias toward first (baseline) wins every sample.
        assert choice == pw.BASELINE

    def test_majority_vote_of_content_preference(self):
        # A content-based judge that prefers the candidate regardless of position.
        def prefers_candidate(prompt):
            # candidate text is "GOOD"; whichever slot holds GOOD is chosen.
            a_start = prompt.index("Response A:")
            b_start = prompt.index("Response B:")
            a_text = prompt[a_start:b_start]
            return "Choice: A" if "GOOD" in a_text else "Choice: B"

        cfg = pw.PairwiseConfig(sampling_count=4, flip_enabled=True)
        choice = pw.judge_case("q", "meh", "GOOD", prefers_candidate, cfg)
        assert choice == pw.CANDIDATE


class TestRunPairwiseEval:
    def _fake_client(self, mapping):
        """A client whose evals.run_inference returns per-engine responses."""

        class _Evals:
            def run_inference(self, agent=None, src=None):
                rows = [
                    {"prompt": r["prompt"], "response": mapping[agent][r["prompt"]]}
                    for _, r in src.iterrows()
                ]
                return pd.DataFrame(rows)

        class _Client:
            evals = _Evals()

        return _Client()

    def test_end_to_end_candidate_wins(self):
        cases = [{"prompt": "q1"}, {"prompt": "q2"}]
        mapping = {
            "BASE": {"q1": "weak", "q2": "weak"},
            "CAND": {"q1": "STRONG", "q2": "STRONG"},
        }
        client = self._fake_client(mapping)

        def judge(prompt):
            a_start = prompt.index("Response A:")
            b_start = prompt.index("Response B:")
            a_text = prompt[a_start:b_start]
            return "Choice: A" if "STRONG" in a_text else "Choice: B"

        result = pw.run_pairwise_eval(
            "BASE",
            "CAND",
            cases=cases,
            client=client,
            generate_fn=judge,
            warm=False,
        )
        assert result["win_rate_candidate"] == pytest.approx(1.0)
        assert result["win_rate_baseline"] == 0.0
        assert len(result["per_case"]) == 2
        assert result["per_case"][0]["choice"] == pw.CANDIDATE

    def test_skips_error_responses(self):
        cases = [{"prompt": "q1"}, {"prompt": "q2"}]
        mapping = {
            "BASE": {"q1": "ok", "q2": ""},  # q2 baseline empty -> skip
            "CAND": {"q1": "ok", "q2": "ok"},
        }
        client = self._fake_client(mapping)
        result = pw.run_pairwise_eval(
            "BASE",
            "CAND",
            cases=cases,
            client=client,
            generate_fn=lambda p: "Choice: TIE",
            warm=False,
        )
        # Only q1 had both responses -> only one judged case.
        assert result["n_cases"] == 1


class TestManifest:
    def test_load_engines_picks_gemini_baseline_claude_candidate(self):
        manifest = {
            "points": [
                {"assignments": {"model_backend": "claude"}, "engine_id": "ENG_CLAUDE"},
                {"assignments": {"model_backend": "gemini"}, "engine_id": "ENG_GEMINI"},
            ]
        }
        baseline, candidate = pw.load_engines_from_manifest(manifest)
        assert baseline == "ENG_GEMINI"  # gemini is baseline (coded -1)
        assert candidate == "ENG_CLAUDE"  # claude is candidate (coded +1)

    def test_missing_engine_id_raises(self):
        manifest = {"points": [{"assignments": {"model_backend": "gemini"}}]}
        with pytest.raises(ValueError):
            pw.load_engines_from_manifest(manifest)


class TestCliEngineResolution:
    def test_dry_run_resolves_bare_ids_to_full_resource_names(self, capsys):
        rc = pw.main(["--baseline", "12345", "--candidate", "67890", "--dry-run"])
        assert rc == 0
        out = capsys.readouterr().out
        # Bare CLI ids must be resolved to the full resource name run_inference needs.
        assert "reasoningEngines/12345" in out
        assert "reasoningEngines/67890" in out
        assert "baseline (gemini):  projects/" in out
        assert "candidate (claude): projects/" in out

    def test_dry_run_keeps_full_resource_names_unchanged(self, capsys):
        full = "projects/p/locations/us-central1/reasoningEngines/abc"
        rc = pw.main(["--baseline", full, "--candidate", full, "--dry-run"])
        assert rc == 0
        out = capsys.readouterr().out
        # Idempotent: an already-full name is passed through verbatim (no double-wrap).
        assert "reasoningEngines/reasoningEngines" not in out
        assert out.count(full) == 2
