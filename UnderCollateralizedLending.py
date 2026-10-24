# { "Depends": "py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6" }

from genlayer import *
import json


class UnderCollateralizedLending(gl.Contract):
    owner: Address
    loan_ceiling_usd: u256
    min_credit_score: u256
    initialized: bool
    applications: TreeMap[Address, str]

    def __init__(self):
        self.owner = gl.message.sender_address
        self.loan_ceiling_usd = u256(5000)
        self.min_credit_score = u256(60)
        self.initialized = True

    @gl.public.write
    def update_parameters(self, new_ceiling_usd: int, new_min_score: int) -> None:
        if gl.message.sender_address != self.owner:
            raise Exception("Only the owner can update parameters")
        if new_min_score < 0 or new_min_score > 100:
            raise Exception("min_score must be 0 to 100")
        if new_ceiling_usd <= 0:
            raise Exception("ceiling_usd must be positive")
        self.loan_ceiling_usd = u256(new_ceiling_usd)
        self.min_credit_score = u256(new_min_score)

    @gl.public.write
    def apply_for_loan(
        self,
        github_username: str,
        ethereum_address: str,
        forum_username: str,
        requested_usd: int,
        collateral_pct: int,
    ) -> None:
        if collateral_pct < 0 or collateral_pct > 100:
            raise Exception("collateral_pct must be 0 to 100")
        if requested_usd <= 0:
            raise Exception("requested_usd must be positive")
        if requested_usd > int(self.loan_ceiling_usd):
            raise Exception("Requested amount exceeds the loan ceiling")

        # Snapshot contract parameters so every validator uses the same values
        # regardless of any concurrent update_parameters call.
        ceiling_usd = int(self.loan_ceiling_usd)
        min_score = int(self.min_credit_score)
        borrower = gl.message.sender_address

        def get_score():
            """
            Fetch external signals, score the borrower with an LLM, then apply
            ALL business rules (min-score gate, ceiling cap, denial zeroing) so
            that the JSON returned here IS the final record to be stored.

            Every validator runs this function independently and must reach the
            same final decision — not just the same raw AI score.
            """
            try:
                github_profile = gl.nondet.web.get(
                    "https://api.github.com/users/" + github_username
                )
            except Exception:
                github_profile = "unavailable"

            try:
                github_repos = gl.nondet.web.get(
                    "https://api.github.com/users/" + github_username
                    + "/repos?sort=stars&per_page=5"
                )
            except Exception:
                github_repos = "unavailable"

            try:
                onchain_txs = gl.nondet.web.get(
                    "https://api.etherscan.io/api?module=account&action=txlist"
                    + "&address=" + ethereum_address
                    + "&sort=desc&offset=10&page=1"
                )
            except Exception:
                onchain_txs = "unavailable"

            try:
                forum_profile = gl.nondet.web.get(
                    "https://gov.uniswap.org/u/" + forum_username + ".json"
                )
            except Exception:
                forum_profile = "unavailable"

            prompt = (
                "You are a DeFi credit analyst scoring a borrower for an under-collateralized loan.\n"
                "Loan requested: $" + str(requested_usd) + " USD. "
                "Collateral offered: " + str(collateral_pct) + "%.\n\n"
                "Score the borrower out of 100 using these criteria:\n"
                "- GitHub reputation (repos, stars, account age, org memberships): 0 to 30 points\n"
                "- On-chain history (tx count, age, no hacks or liquidations): 0 to 30 points\n"
                "- Governance forum (proposals, replies, trust level, tenure): 0 to 20 points\n"
                "- Collateral offered (100 pct = 20 pts, 0 pct = 0 pts, linear): 0 to 20 points\n\n"
                "Deduct heavily for: brand-new accounts, zero activity, suspicious patterns.\n\n"
                "GITHUB PROFILE:\n" + str(github_profile)[:1200] + "\n\n"
                "GITHUB TOP REPOS:\n" + str(github_repos)[:1200] + "\n\n"
                "ON-CHAIN TRANSACTIONS:\n" + str(onchain_txs)[:1200] + "\n\n"
                "FORUM PROFILE:\n" + str(forum_profile)[:1200] + "\n\n"
                "Respond with ONLY valid JSON and no extra text:\n"
                "{\"credit_score\": <integer 0-100>, \"approved\": <true or false>, "
                "\"max_loan_usd\": <integer whole dollars, must not exceed " + str(ceiling_usd) + ">, "
                "\"interest_rate_bps\": <integer e.g. 800 means 8 percent APR>, "
                "\"reason\": \"<one sentence>\"}"
            )

            raw = gl.nondet.exec_prompt(prompt)
            backticks = "```"
            raw = raw.replace(backticks + "json", "").replace(backticks, "").strip()
            parsed = json.loads(raw)

            # --- Sanitise raw AI output ---
            credit_score = int(parsed.get("credit_score") or 0)
            credit_score = max(0, min(100, credit_score))
            ai_approved = bool(parsed.get("approved", False))
            max_loan_usd = max(0, int(parsed.get("max_loan_usd") or 0))
            rate_bps = max(0, int(parsed.get("interest_rate_bps") or 1500))
            reason = str(parsed.get("reason") or parsed.get("reasoning") or "")

            # --- Apply business rules to produce the FINAL stored values ---
            #
            # Rule 1: the min-score gate is deterministic from credit_score and
            #         min_score; it must be decided here so validators bind it.
            score_passes = credit_score >= min_score

            # Rule 2: both the AI and the score gate must approve.
            final_approved = ai_approved and score_passes

            if final_approved:
                # Rule 3: the loan ceiling is an absolute cap on max_loan_usd.
                max_loan_usd = min(max_loan_usd, ceiling_usd)
            else:
                # Rule 4: a denial zeroes all consequential financial terms.
                max_loan_usd = 0
                rate_bps = 0
                if not score_passes:
                    reason = (
                        "Score " + str(credit_score)
                        + " is below the minimum of " + str(min_score)
                        + ". " + reason
                    )

            return json.dumps({
                "credit_score": credit_score,
                "approved": final_approved,
                "max_loan_usd": max_loan_usd,
                "interest_rate_bps": rate_bps,
                "reason": reason,
            })

        def validate_score(leader_result) -> bool:
            """
            Each validator re-runs get_score independently and compares its own
            final output against the leader's.  Every field that ends up stored
            on-chain is validated:

            - approved        exact match (the commit/deny decision)
            - credit_score    within ±10 (inherent LLM variance)
            - max_loan_usd    within ±5 % of the larger value, or both zero
            - interest_rate_bps  within ±100 bps, or both zero

            Because business rules run inside get_score, a validator that
            disagrees about the approval decision, the ceiling cap, or the
            denial zeroing will always produce a mismatched `approved` field
            and will reject the leader's result.
            """
            if not isinstance(leader_result, gl.vm.Return):
                return False
            try:
                leader = json.loads(leader_result.calldata)
                mine = json.loads(get_score())
            except Exception:
                return False

            # Approval must match exactly — this is the binary commit/deny.
            if mine["approved"] != leader["approved"]:
                return False

            # Credit score may vary slightly between validator LLM calls.
            if abs(mine["credit_score"] - leader["credit_score"]) > 10:
                return False

            # Financial terms must agree within a tight tolerance.
            # If both are zero (denial) the checks below pass automatically.
            l_loan = int(leader["max_loan_usd"])
            m_loan = int(mine["max_loan_usd"])
            if l_loan == 0 and m_loan == 0:
                loan_ok = True
            else:
                larger_loan = max(l_loan, m_loan)
                loan_ok = abs(l_loan - m_loan) <= max(1, int(larger_loan * 0.05))

            l_rate = int(leader["interest_rate_bps"])
            m_rate = int(mine["interest_rate_bps"])
            rate_ok = abs(l_rate - m_rate) <= 100

            return loan_ok and rate_ok

        scored_json = gl.vm.run_nondet_unsafe(get_score, validate_score)
        scored = json.loads(scored_json)

        # The consensus payload is already the final record; store it directly.
        self.applications[borrower] = json.dumps({
            "borrower": borrower.as_hex,
            "github_username": github_username,
            "ethereum_address": ethereum_address,
            "forum_username": forum_username,
            "requested_usd": requested_usd,
            "collateral_pct": collateral_pct,
            "credit_score": int(scored["credit_score"]),
            "approved": bool(scored["approved"]),
            "max_loan_usd": int(scored["max_loan_usd"]),
            "interest_rate_bps": int(scored["interest_rate_bps"]),
            "reason": str(scored["reason"]),
        })

    @gl.public.view
    def get_application(self, borrower_address: str) -> str:
        addr = Address(borrower_address)
        if addr not in self.applications:
            return json.dumps({"error": "No application found"})
        return self.applications[addr]

    @gl.public.view
    def get_credit_score(self, borrower_address: str) -> int:
        addr = Address(borrower_address)
        if addr not in self.applications:
            return 0
        app = json.loads(self.applications[addr])
        return int(app["credit_score"])

    @gl.public.view
    def is_approved(self, borrower_address: str) -> bool:
        addr = Address(borrower_address)
        if addr not in self.applications:
            return False
        app = json.loads(self.applications[addr])
        return bool(app["approved"])

    @gl.public.view
    def get_loan_terms(self, borrower_address: str) -> str:
        addr = Address(borrower_address)
        if addr not in self.applications:
            return json.dumps({
                "approved": False,
                "max_loan_usd": 0,
                "interest_rate_bps": 0,
                "credit_score": 0,
            })
        app = json.loads(self.applications[addr])
        if not app["approved"]:
            return json.dumps({
                "approved": False,
                "max_loan_usd": 0,
                "interest_rate_bps": 0,
                "credit_score": int(app["credit_score"]),
                "reason": str(app["reason"]),
            })
        return json.dumps({
            "approved": True,
            "max_loan_usd": int(app["max_loan_usd"]),
            "interest_rate_bps": int(app["interest_rate_bps"]),
            "credit_score": int(app["credit_score"]),
            "reason": str(app["reason"]),
        })

    @gl.public.view
    def get_config(self) -> str:
        return json.dumps({
            "owner": self.owner.as_hex,
            "loan_ceiling_usd": int(self.loan_ceiling_usd),
            "min_credit_score": int(self.min_credit_score),
        })
