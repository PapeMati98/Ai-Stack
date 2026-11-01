"""Unit tests for Personal Brand, Marketing & Sales department playbook in business-capabilities."""

from __future__ import annotations

from pathlib import Path
import re
import unittest

BASE_DIR = Path(__file__).resolve().parent.parent
SKILL_MD_PATH = BASE_DIR / "SKILL.md"
REFS_DIR = BASE_DIR / "references"
CAPABILITY_MAP_PATH = REFS_DIR / "capability-map.md"
PLAYBOOK_PATH = REFS_DIR / "dzial-marka-marketing-sprzedaz.md"


class TestMarketingDepartmentPlaybookFiles(unittest.TestCase):
    """Verify presence and basic readability of all required files."""

    def test_files_exist(self) -> None:
        self.assertTrue(SKILL_MD_PATH.is_file(), f"Missing {SKILL_MD_PATH}")
        self.assertTrue(CAPABILITY_MAP_PATH.is_file(), f"Missing {CAPABILITY_MAP_PATH}")
        self.assertTrue(PLAYBOOK_PATH.is_file(), f"Missing {PLAYBOOK_PATH}")

    def test_playbook_not_empty(self) -> None:
        content = PLAYBOOK_PATH.read_text(encoding="utf-8")
        self.assertGreater(len(content.strip()), 500)


class TestSkillAndCapabilityMapRouting(unittest.TestCase):
    """Verify SKILL.md and capability-map.md route to dzial-marka-marketing-sprzedaz.md."""

    def setUp(self) -> None:
        self.skill_content = SKILL_MD_PATH.read_text(encoding="utf-8")
        self.map_content = CAPABILITY_MAP_PATH.read_text(encoding="utf-8")

    def test_skill_md_links_playbook(self) -> None:
        self.assertIn("dzial-marka-marketing-sprzedaz.md", self.skill_content)
        self.assertIn("Personal Brand, Marketing & Sales", self.skill_content)

    def test_skill_md_enforces_t2_authority_boundary(self) -> None:
        self.assertIn("T2", self.skill_content)
        self.assertIn("mandatory Mateusz consent", self.skill_content)

    def test_capability_map_links_playbook(self) -> None:
        self.assertIn("dzial-marka-marketing-sprzedaz.md", self.map_content)
        self.assertIn("Personal Brand, Marketing & Sales", self.map_content)

    def test_capability_map_table_entry(self) -> None:
        # Table must contain row with dzial-marka-marketing-sprzedaz.md
        table_pattern = re.compile(
            r"\|.*Personal Brand.*\|.*dzial-marka-marketing-sprzedaz\.md.*\|",
            re.IGNORECASE,
        )
        self.assertTrue(
            table_pattern.search(self.map_content),
            "capability-map.md table does not contain Personal Brand entry routing to dzial-marka-marketing-sprzedaz.md",
        )


class TestSpecialistSkillLinkages(unittest.TestCase):
    """Verify that dzial-marka-marketing-sprzedaz.md references all required skills and tools."""

    def setUp(self) -> None:
        self.playbook = PLAYBOOK_PATH.read_text(encoding="utf-8")

    def test_core_strategy_and_growth_skills(self) -> None:
        self.assertIn("marketing-context", self.playbook)
        self.assertIn("search-visibility", self.playbook)
        self.assertIn("google-ads-strategy", self.playbook)
        self.assertIn("meta-ads-strategy", self.playbook)
        self.assertIn("growth-experiments", self.playbook)

    def test_media_production_skills(self) -> None:
        self.assertIn("video-gen", self.playbook)
        self.assertIn("avatar-gen", self.playbook)
        self.assertIn("voice-gen", self.playbook)
        self.assertIn("audio-bed-gen", self.playbook)
        self.assertIn("html-video", self.playbook)

    def test_email_and_tone_of_voice(self) -> None:
        self.assertIn("email", self.playbook)
        self.assertIn("mateusz-style-profile.json", self.playbook)
        # Two-stage workflow: draft -> token -> send-approved
        self.assertIn("draft", self.playbook.lower())
        self.assertIn("send-approved", self.playbook.lower())
        self.assertTrue(
            "approval token" in self.playbook.lower()
            or "owner approval token" in self.playbook.lower()
            or "token" in self.playbook.lower()
        )

    def test_financing_and_investor_acceptance(self) -> None:
        self.assertIn("fundraising", self.playbook)
        self.assertIn("review-investor-materials", self.playbook)

    def test_search_and_citation_engines(self) -> None:
        # Check SEO, GEO, AEO, LLM citations
        self.assertIn("SEO", self.playbook)
        self.assertIn("GEO", self.playbook)
        self.assertIn("AEO", self.playbook)


class TestT2ConsentGates(unittest.TestCase):
    """Verify explicit T2 IN/OUT consent table and fail-closed rules."""

    def setUp(self) -> None:
        self.playbook = PLAYBOOK_PATH.read_text(encoding="utf-8")

    def test_t2_consent_table_headers(self) -> None:
        self.assertIn("DEV / TEST", self.playbook)
        self.assertIn("Mateusz IN", self.playbook)
        self.assertIn("Mateusz OUT", self.playbook)

    def test_autonomous_actions_in_dev_test(self) -> None:
        # Autonomous scope in DEV/TEST: research, ICP, SEO/GEO audit, copy/landing drafts, media briefs, email drafts
        self.assertIn("research", self.playbook.lower())
        self.assertIn("icp", self.playbook.lower())
        self.assertIn("seo", self.playbook.lower())
        self.assertIn("draft", self.playbook.lower())

    def test_mandatory_mateusz_consent_actions(self) -> None:
        # Mandatory consent: publication, ad budget/launch, email send/outreach, pricing/offer
        lower_pb = self.playbook.lower()
        self.assertTrue("publikacja" in lower_pb or "publication" in lower_pb)
        self.assertTrue("budżet" in lower_pb or "budget" in lower_pb)
        self.assertTrue("wysyłka" in lower_pb or "outreach" in lower_pb or "send" in lower_pb)
        self.assertTrue("cenow" in lower_pb or "pricing" in lower_pb or "ofert" in lower_pb)

    def test_fail_closed_and_absence_rule(self) -> None:
        # When Mateusz is OUT, external actions fail-closed
        self.assertIn("fail-closed", self.playbook.lower())
        self.assertIn("blokada", self.playbook.lower())


class TestPilotPackageTemplates(unittest.TestCase):
    """Verify presence and structure of all 3 pilot package templates."""

    def setUp(self) -> None:
        self.playbook = PLAYBOOK_PATH.read_text(encoding="utf-8")
        self.lower_pb = self.playbook.lower()

    def test_pilot_a_personal_brand_and_video(self) -> None:
        self.assertIn("Pilot A", self.playbook)
        self.assertIn("Personal Brand", self.playbook)
        self.assertIn("LinkedIn", self.playbook)
        # Content pillars, post draft, media brief, tone of voice check
        lower_pb = self.playbook.lower()
        self.assertIn("content pillars", lower_pb)
        self.assertTrue("hook" in lower_pb or "haczyk" in lower_pb)
        self.assertIn("mateusz-style-profile.json", self.playbook)

    def test_pilot_b_b2b_product_funnel_and_ads(self) -> None:
        self.assertIn("Pilot B", self.playbook)
        self.assertIn("Funnel", self.playbook)
        self.assertIn("Google Ads", self.playbook)
        self.assertIn("Meta Ads", self.playbook)
        # Funnel stages, search visibility, CRO experiment, budget gate
        self.assertIn("ToFU", self.playbook)
        self.assertIn("MoFU", self.playbook)
        self.assertIn("BoFU", self.playbook)
        self.assertIn("growth-experiments", self.playbook)
        # Account status must be unverified/pending, not invented pre-verified staging state
        self.assertNotIn("staging_verified", self.lower_pb)
        self.assertTrue(
            "pending_verification" in self.lower_pb
            or "oczekuje na weryfikację" in self.lower_pb
            or "oczekuje_na_weryfikację" in self.lower_pb
        )

    def test_pilot_c_investor_one_pager_and_outbound(self) -> None:
        self.assertIn("Pilot C", self.playbook)
        self.assertIn("One-Pager", self.playbook)
        self.assertIn("fundraising", self.playbook)
        self.assertIn("review-investor-materials", self.playbook)
        # Outbound email draft and two-stage gate
        self.assertIn("mateusz-style-profile.json", self.playbook)
        self.assertTrue("approval_token" in self.playbook or "token" in self.playbook.lower())


class TestPlaybookGuardrailsAndIntegrity(unittest.TestCase):
    """Verify guardrails against unverified claims, pre-completed reviews, and fake mechanisms."""

    def setUp(self) -> None:
        self.playbook = PLAYBOOK_PATH.read_text(encoding="utf-8")
        self.lower_pb = self.playbook.lower()

    def test_no_precompleted_review_checkboxes(self) -> None:
        """Review checklists and investor acceptance checklists must not be pre-completed with [x]."""
        self.assertNotIn("[x] Spójność matematyczna", self.playbook)
        self.assertNotIn("[x] Sprawdzenie poufności", self.playbook)
        self.assertNotIn("[x] Zero pustosłowia", self.playbook)
        # Check that checklists use unchecked boxes [ ]
        self.assertIn("[ ]", self.playbook)

    def test_investor_acceptance_pending_independent_review(self) -> None:
        """Investor acceptance must be marked as pending independent review."""
        self.assertIn("review-investor-materials", self.playbook)
        self.assertTrue(
            "pending_independent_review" in self.lower_pb
            or "oczekuje na niezależny odbiór" in self.lower_pb
        )

    def test_email_gate_fail_closed_sequence(self) -> None:
        """Email gate must define the sequence: draft -> owner approval token -> send-approved."""
        self.assertIn("draft", self.lower_pb)
        self.assertTrue("owner approval token" in self.lower_pb or "approval token" in self.lower_pb)
        self.assertIn("send-approved", self.lower_pb)
        self.assertIn("fail-closed", self.lower_pb)

    def test_no_fake_hardware_interlock_claims(self) -> None:
        """Must not claim an unimplemented hardware ban on connector send()."""
        self.assertNotIn("sprzętowy zakaz", self.lower_pb)

    def test_no_fake_technical_gateway_claims(self) -> None:
        """Must not claim unimplemented technical gateway blocks on email or payment."""
        self.assertNotIn("techniczna blokada bramek", self.lower_pb)
        self.assertNotIn("techniczną blokadą bramek", self.lower_pb)
        self.assertNotIn("techniczna blokada", self.lower_pb)
        self.assertNotIn("techniczną blokadą", self.lower_pb)
        # Playbook must rely on agent-side fail-closed refusal
        self.assertTrue(
            "odmow" in self.lower_pb
            or "logice agent" in self.lower_pb
        )

    def test_no_invented_staging_verified_status(self) -> None:
        """Must not invent system state such as STAGING_VERIFIED without an actual staging check."""
        self.assertNotIn("staging_verified", self.lower_pb)
        self.assertTrue(
            "pending_verification" in self.lower_pb
            or "oczekuje_na_weryfikację" in self.lower_pb
            or "oczekuje na weryfikację" in self.lower_pb
        )

    def test_pilot_templates_prohibit_unverified_claims(self) -> None:
        """Playbook must state rules prohibiting unverified metrics, system state, or legal claims."""
        self.assertTrue(
            "niezweryfikowan" in self.lower_pb
            or "zabrania się" in self.lower_pb
            or "zasada rzetelności" in self.lower_pb
        )


if __name__ == "__main__":
    unittest.main()
