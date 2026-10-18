# UnderCollateralizedLending

A [GenLayer](https://genlayer.com) intelligent contract that scores borrowers for under-collateralized loans using off-chain reputation data and an LLM-based credit analysis, with validator consensus on the result.

Instead of relying only on posted collateral, the contract looks at a borrower's GitHub history, on-chain activity, and governance forum participation, then asks an LLM to produce a credit score and loan terms.

## How It Works

1. A borrower calls `apply_for_loan` with their GitHub username, Ethereum address, Uniswap governance forum username, requested amount (USD), and the percentage of collateral they are offering.
2. The contract fetches, via `gl.nondet.web.get`:
   - GitHub profile: `https://api.github.com/users/<username>`
   - GitHub top repos (by stars, top 5)
   - Recent Ethereum transactions from the Etherscan API
   - Governance forum profile: `https://gov.uniswap.org/u/<username>.json`
   - If any fetch fails, that source is recorded as `"unavailable"` and scoring continues.
3. An LLM prompt (`gl.nondet.exec_prompt`) scores the borrower out of 100:

   | Category | Max points |
   |---|---|
   | GitHub reputation (repos, stars, age, orgs) | 30 |
   | On-chain history (tx count, age, no hacks/liquidations) | 30 |
   | Governance forum (proposals, replies, trust level, tenure) | 20 |
   | Collateral offered (linear: 100% = 20, 0% = 0) | 20 |

   The model also returns `approved`, `max_loan_usd`, `interest_rate_bps`, and a one-sentence `reason`.
4. Validators reach consensus through `gl.vm.run_nondet_unsafe` with a custom validator (see below).
5. The contract applies its own policy on top of the LLM output and stores the result per borrower.

## Consensus Rules

Because LLM output is non-deterministic, each validator independently re-runs the scoring and compares against the leader's result. A result is accepted only if:

- the credit scores differ by **10 points or fewer**, and
- the `approved` flag **matches** exactly.

A leader result that is not a successful return is rejected.

## Final Decision Logic

The stored `approved` value is:

```
final_approved = ai_approved AND (credit_score >= min_credit_score)
```

- If approved, `max_loan_usd` is capped at `loan_ceiling_usd`.
- If not approved, `max_loan_usd` and `interest_rate_bps` are set to `0`. If the score is below the minimum, the reason is prefixed with `Score X is below the minimum of Y.`

## Default Configuration

| Parameter | Default | Description |
|---|---|---|
| `loan_ceiling_usd` | `5000` | Maximum loan amount and cap on any approved loan |
| `min_credit_score` | `60` | Minimum score required for approval |

The deployer becomes the `owner`.

## Contract API

### Write methods

#### `update_parameters(new_ceiling_usd: int, new_min_score: int)`
Owner only. Updates the loan ceiling and minimum credit score.
- `new_min_score` must be 0 to 100
- `new_ceiling_usd` must be positive

#### `apply_for_loan(github_username, ethereum_address, forum_username, requested_usd, collateral_pct)`
Submits an application for the calling address. Reverts if:
- `collateral_pct` is outside 0 to 100
- `requested_usd` is not positive
- `requested_usd` exceeds `loan_ceiling_usd`

Re-applying overwrites the borrower's previous application.

### View methods

| Method | Returns |
|---|---|
| `get_application(borrower_address)` | Full application JSON, or `{"error": "No application found"}` |
| `get_credit_score(borrower_address)` | Credit score (int), `0` if none |
| `is_approved(borrower_address)` | `bool`, `False` if none |
| `get_loan_terms(borrower_address)` | JSON with `approved`, `max_loan_usd`, `interest_rate_bps`, `credit_score`, `reason` |
| `get_config()` | JSON with `owner`, `loan_ceiling_usd`, `min_credit_score` |

### Stored application format

```json
{
  "borrower": "0x...",
  "github_username": "octocat",
  "ethereum_address": "0x...",
  "forum_username": "someuser",
  "requested_usd": 2000,
  "collateral_pct": 25,
  "credit_score": 72,
  "approved": true,
  "max_loan_usd": 2000,
  "interest_rate_bps": 800,
  "reason": "Strong GitHub and governance history."
}
```

`interest_rate_bps` is in basis points (800 = 8% APR).

## Usage Example

Using the GenLayer CLI (adjust to your tooling):

```bash
# Deploy
genlayer deploy --contract UnderCollateralizedLending.py

# Apply for a loan
genlayer write <CONTRACT_ADDRESS> apply_for_loan \
  --args "octocat" "0xYourEthAddress" "forum_user" 2000 25

# Check the outcome
genlayer call <CONTRACT_ADDRESS> get_loan_terms --args <BORROWER_ADDRESS>
```

## Important Notes and Limitations

- **This contract only scores and records terms.** It does not custody funds, disburse loans, track repayment, or liquidate collateral. It is intended as a credit-decision layer to be integrated with a lending protocol.
- **Self-reported inputs.** Nothing proves the applicant owns the supplied GitHub account, Ethereum address, or forum account. A borrower could submit someone else's strong profiles. Consider adding ownership verification (e.g. a signed message, a gist, or a forum post challenge) before relying on scores.
- **`collateral_pct` is not enforced or verified on-chain**; it is an input to the score only.
- **Etherscan API** is called without an API key and uses the legacy `api.etherscan.io/api` endpoint, so it may be rate limited or deprecated. Add a key or switch endpoints for production use.
- **Input handling.** Usernames and addresses are concatenated directly into URLs and each source is truncated to 1,200 characters in the prompt. Consider validating inputs and stripping untrusted text to reduce prompt-injection risk from profile content.
- **LLM variance.** Even with the consensus tolerance, scores are subjective and may differ between runs.
- **Public data.** Applications, including usernames and addresses, are stored on-chain and publicly readable.

## Requirements

- GenLayer runtime with `py-genlayer` (pinned in the contract header dependency hash)
- Network access for validators to GitHub, Etherscan, and the Uniswap governance forum

## License

Add your preferred license here.
