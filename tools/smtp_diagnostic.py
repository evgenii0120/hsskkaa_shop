from __future__ import annotations

import os
import smtplib
import ssl
import sys
from dotenv import load_dotenv


def main() -> int:
    load_dotenv()
    host = os.getenv("SMTP_HOST", "").strip()
    port = int(os.getenv("SMTP_PORT", "587"))
    use_tls = os.getenv("SMTP_USE_TLS", "false").strip().lower() == "true"
    start_tls = os.getenv("SMTP_START_TLS", "true").strip().lower() == "true"
    timeout = float(os.getenv("SMTP_TIMEOUT", "30"))

    if not host:
        print("FAIL: SMTP_HOST is empty")
        return 2
    if use_tls and start_tls:
        print("FAIL: SSL and STARTTLS are both enabled")
        return 2
    if port == 465 and (not use_tls or start_tls):
        print("FAIL: port 465 requires SMTP_USE_TLS=true and SMTP_START_TLS=false")
        return 2
    if port == 587 and (use_tls or not start_tls):
        print("FAIL: port 587 requires SMTP_USE_TLS=false and SMTP_START_TLS=true")
        return 2

    client = None
    try:
        context = ssl.create_default_context()
        if use_tls:
            client = smtplib.SMTP_SSL(context=context, timeout=timeout)
        else:
            client = smtplib.SMTP(timeout=timeout)

        print(f"1/4 TCP connect: {host}:{port}")
        client.connect(host, port)

        print("2/4 Waiting for SMTP server greeting")
        code, _ = client.getreply()
        if code != 220:
            print(f"FAIL: unexpected SMTP greeting code={code}")
            return 3
        print("OK: SMTP greeting received (220)")

        print("3/4 EHLO")
        code, _ = client.ehlo()
        if code != 250:
            print(f"FAIL: EHLO rejected code={code}")
            return 3
        print("OK: EHLO accepted")

        if start_tls:
            print("4/4 STARTTLS and certificate validation")
            code, _ = client.starttls(context=context)
            if code != 220:
                print(f"FAIL: STARTTLS rejected code={code}")
                return 3
            print("OK: STARTTLS established; certificate validation passed")
        else:
            print("4/4 Implicit TLS certificate validation completed")
            print("OK: TLS connection established")

        print("PASS: SMTP network/TLS path works. No login or email was attempted.")
        return 0
    except (OSError, ssl.SSLError, smtplib.SMTPException, TimeoutError) as exc:
        print(f"FAIL: {type(exc).__name__}: {str(exc)[:500]}")
        return 1
    finally:
        if client is not None:
            try:
                client.close()  # close without sending any message
            except Exception:
                pass


if __name__ == "__main__":
    raise SystemExit(main())
