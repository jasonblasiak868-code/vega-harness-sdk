# SECURITY

Report a problem to jasonblasiak.ai.mail@gmail.com. Please do not post an exploitable detail in a public issue.

## Threat model in one paragraph

The code is meant to stop an AI or automated process from taking an action no human approved. Assumptions: the human's private key never touches the machine that runs the model; the machine's clock and disk are not controlled by the attacker; every path that can cause an effect is routed through the gates. It does not defend against a compromised operating system, a stolen human key, or a bypass path that was never wired through the gate.

## What was done

An automated review (REVIEW.md) ran static rules, secret-shape checks and the tests; hand-written red-team tests are in tests/ where present. None of the cryptography or protocol code has had an independent expert review. A clean scan is not proof of safety.

## Secrets

No credential belongs in this repo. Run a secret scanner (for example gitleaks) before every commit.
