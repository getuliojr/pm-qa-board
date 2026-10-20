# Security Policy

## Supported Versions

Only the latest commit on the `main` branch is actively maintained.

| Version | Supported |
|---------|-----------|
| `main` (latest) | ✅ |
| older commits | ❌ |

## Reporting a Vulnerability

**Please do not open a public GitHub issue for security vulnerabilities.**

To report a security issue, use the **[GitHub private vulnerability reporting](https://github.com/getuliojr/pm-qa-board/security/advisories/new)** feature on this repository.

Please include:

- A clear description of the vulnerability
- Steps to reproduce
- Potential impact / attack scenario
- Any suggested mitigations (optional)

You can expect an initial response within **72 hours**.

## Scope

This tool is a **local-only development utility** (runs on `127.0.0.1:3333`). It is not designed to be exposed to the public internet. The following are considered in scope:

- Local privilege escalation via the server API
- Path traversal vulnerabilities in the file-serving layer
- Token bypass or authentication weaknesses

The following are **out of scope**:

- Vulnerabilities requiring physical access to the machine
- Issues in third-party CDN dependencies (Tailwind, Phosphor Icons, Marked, DOMPurify)
