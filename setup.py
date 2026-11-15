"""
Connects the bot to OANDA. Asks only for the API token, then finds your accounts
itself so there is no account number to type.

Run by install.sh:   python3 setup.py practice     (or: live)
"""
import json
import os
import re
import sys
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
HOSTS = {"practice": "https://api-fxpractice.oanda.com", "live": "https://api-fxtrade.oanda.com"}


def get(host, token, path):
    """Returns (status, data). status 200 = fine, 401/403 = OANDA said no, 0 = no internet."""
    req = urllib.request.Request(host + path, headers={"Authorization": "Bearer " + token})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return 200, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, {}
    except Exception:
        return 0, {}


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "practice"
    other = "live" if mode == "practice" else "practice"
    raw = input("\nPaste your OANDA %s API token, then press Enter: " % mode)
    token = re.sub(r"[^0-9A-Za-z-]", "", raw)          # drop spaces and invisible characters
    if not re.fullmatch(r"[0-9a-f]{32}-[0-9a-f]{32}", token):
        print("\nThat does not look like a complete token (got %d characters, expected 65:" % len(token))
        print("two blocks of 32 letters/numbers joined by a dash). Copy it again and re-run this.")
        return 1

    status, data = get(HOSTS[mode], token, "/v3/accounts")
    if status == 0:
        print("\nThis server could not reach OANDA at all. Try again in a minute.")
        return 1
    if status != 200:
        s2, _ = get(HOSTS[other], token, "/v3/accounts")
        if s2 == 200:
            print("\nThis token belongs to your OANDA %s login, not the %s one." % (other.upper(), mode.upper()))
            if mode == "practice":
                print("Log in to the DEMO side of OANDA (the fxTrade Practice login), generate the token there,")
                print("and re-run this.")
            else:
                print("Generate the token while logged in to your LIVE account, and re-run this.")
        else:
            print("\nOANDA does not recognise this token on either the demo or the live side.")
            print("It was probably replaced by a newer one. In OANDA: Manage API Access -> Revoke -> Generate,")
            print("copy the new token straight away, and re-run this.")
        return 1

    usable = []
    print("\nToken accepted. Accounts it can see:")
    for a in data.get("accounts", []):
        st, d = get(HOSTS[mode], token, "/v3/accounts/%s/summary" % a["id"])
        tags = ",".join(a.get("tags", []))
        if st == 200:
            acct = d["account"]
            usable.append(a["id"])
            print("  %d) %s   balance %s %s   %s" % (len(usable), a["id"], acct["balance"], acct["currency"], tags))
        else:
            print("  -) %s   cannot be used by the bot   %s" % (a["id"], tags))
    if not usable:
        print("\nNone of these accounts can be traded through the API. In OANDA, add a new v20 sub-account")
        print("(not MT4), generate a NEW token afterwards, and re-run this.")
        return 1
    if len(usable) == 1:
        account = usable[0]
        print("\nUsing %s (the only usable account)." % account)
    else:
        pick = input("\nType the number of the account the bot should trade, then press Enter: ").strip()
        if not pick.isdigit() or not (1 <= int(pick) <= len(usable)):
            print("That was not one of the numbers. Re-run this and type e.g. 1")
            return 1
        account = usable[int(pick) - 1]

    path = os.path.join(HERE, ".env")
    with open(path, "w") as f:
        f.write("OANDA_TOKEN=%s\nOANDA_ACCOUNT=%s\nOANDA_ENV=%s\n" % (token, account, mode))
    os.chmod(path, 0o600)
    return 0


if __name__ == "__main__":
    sys.exit(main())
