#!/usr/bin/env python2.7
import argparse
import atexit
import base64
import datetime
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib
import urllib2

import startservers

ISSUANCE_FAILED = 1
REVOCATION_FAILED = 2
MAILER_FAILED = 3

class ExitStatus:
    OK, PythonFailure, NodeFailure, Error, OCSPFailure, CTFailure, IncorrectCommandLineArgs, RevokerFailure = range(8)

JS_DIR = 'test/js'

class ProcInfo:
    """
        Args:
            cmd (str): The command that was run
            proc(subprocess.Popen): The Popen of the command run
    """

    def __init__(self, cmd, proc):
        self.cmd = cmd
        self.proc = proc


def die(status):
    global exit_status
    # Set exit_status so cleanup handler knows what to report.
    exit_status = status
    sys.exit(exit_status)

def fetch_ocsp(request_bytes, url):
    """Fetch an OCSP response using POST, GET, and GET with URL encoding.

    Returns a tuple of the responses.
    """
    ocsp_req_b64 = base64.b64encode(request_bytes)

    # Make the OCSP request three different ways: by POST, by GET, and by GET with
    # URL-encoded parameters. All three should have an identical response.
    get_response = urllib2.urlopen("%s/%s" % (url, ocsp_req_b64)).read()
    get_encoded_response = urllib2.urlopen("%s/%s" % (url, urllib.quote(ocsp_req_b64, safe = ""))).read()
    post_response = urllib2.urlopen("%s/" % (url), request_bytes).read()

    return (post_response, get_response, get_encoded_response)

def make_ocsp_req(cert_file, issuer_file):
    """Return the bytes of an OCSP request for the given certificate file."""
    ocsp_req_file = os.path.join(tempdir, "ocsp.req")
    # First generate the OCSP request in DER form
    cmd = ("openssl ocsp -no_nonce -issuer %s -cert %s -reqout %s" % (
        issuer_file, cert_file, ocsp_req_file))
    print cmd
    subprocess.check_output(cmd, shell=True)
    with open(ocsp_req_file) as f:
        ocsp_req = f.read()
    return ocsp_req

def fetch_until(cert_file, issuer_file, url, initial, final):
    """Fetch OCSP for cert_file until OCSP status goes from initial to final.

    Initial and final are treated as regular expressions. Any OCSP response
    whose OpenSSL OCSP verify output doesn't match either initial or final is
    a fatal error.

    If OCSP responses by the three methods (POST, GET, URL-encoded GET) differ
    from each other, that is a fatal error.

    If we loop for more than five seconds, that is a fatal error.

    Returns nothing on success.
    """
    ocsp_request = make_ocsp_req(cert_file, issuer_file)
    timeout = time.time() + 5
    while True:
        time.sleep(0.25)
        if time.time() > timeout:
            print("Timed out waiting for OCSP to go from '%s' to '%s'" % (
                initial, final))
            die(ExitStatus.OCSPFailure)
        responses = fetch_ocsp(ocsp_request, url)
        # This variable will be true at the end of the loop if all the responses
        # matched the final state.
        all_final = True
        for resp in responses:
            verify_output = ocsp_verify(cert_file, issuer_file, resp)
            if re.search(initial, verify_output):
                all_final = False
                break
            elif re.search(final, verify_output):
                continue
            else:
                print verify_output
                print("OCSP response didn't match '%s' or '%s'" %(
                    initial, final))
                die(ExitStatus.OCSPFailure)
        if all_final:
            # Check that all responses were equal to each other.
            for resp in responses:
                if resp != responses[0]:
                    print "OCSP responses differed:"
                    print(base64.b64encode(responses[0]))
                    print(" vs ")
                    print(base64.b64encode(resp))
                    die(ExitStatus.OCSPFailure)
            return

def ocsp_verify(cert_file, issuer_file, ocsp_response):
    ocsp_resp_file = os.path.join(tempdir, "ocsp.resp")
    with open(ocsp_resp_file, "w") as f:
        f.write(ocsp_response)
    ocsp_verify_cmd = """openssl ocsp -no_nonce -issuer %s -cert %s \
      -verify_other %s -CAfile test/test-root.pem \
      -respin %s""" % (issuer_file, cert_file, issuer_file, ocsp_resp_file)
    print ocsp_verify_cmd
    try:
        output = subprocess.check_output(ocsp_verify_cmd,
            shell=True, stderr=subprocess.STDOUT)
    except subprocess.CalledProcessError as e:
        output = e.output
        print output
        print "subprocess returned non-zero: %s" % e
        die(ExitStatus.OCSPFailure)
    # OpenSSL doesn't always return non-zero when response verify fails, so we
    # also look for the string "Response Verify Failure"
    verify_failure = "Response Verify Failure"
    if re.search(verify_failure, output):
        print output
        die(ExitStatus.OCSPFailure)
    return output

def wait_for_ocsp_good(cert_file, issuer_file, url):
    fetch_until(cert_file, issuer_file, url, " unauthorized", ": good")

def wait_for_ocsp_revoked(cert_file, issuer_file, url):
    fetch_until(cert_file, issuer_file, url, ": good", ": revoked")

def get_expiry_time(cert_file):
    try:
        output = subprocess.check_output(["openssl", "x509", "-enddate", "-noout", "-in", cert_file])
    except subprocess.CalledProcessError as e:
        output = e.output
        print output
        print "subprocess returned non-zero: %s" % e
        die(ExitStatus.NodeFailure)

    return datetime.datetime.strptime(output.split('\n')[0].split('=')[1], '%b %d %H:%M:%S %Y %Z')

def verify_ct_submission(expectedSubmissions, url):
    resp = urllib2.urlopen(url)
    submissionStr = resp.read()
    if int(submissionStr) != expectedSubmissions:
        print "Expected %d submissions, found %d" % (expectedSubmissions, int(submissionStr))
        die(ExitStatus.CTFailure)
    return 0

def run_node_test(domain, chall_type, expected_ct_submissions):
    email_addr = "js.integration.test@letsencrypt.org"
    cert_file = os.path.join(tempdir, "cert.der")
    cert_file_pem = os.path.join(tempdir, "cert.pem")
    key_file = os.path.join(tempdir, "key.pem")
    # Issue the certificate and transform it from DER-encoded to PEM-encoded.
    if subprocess.Popen('''
        node test.js --email %s --domains %s \
          --certKey %s --cert %s --challType %s && \
        openssl x509 -in %s -out %s -inform der -outform pem
        ''' % (email_addr, domain, key_file, cert_file, chall_type, cert_file, cert_file_pem),
        shell=True, cwd=JS_DIR).wait() != 0:
        print("\nIssuing failed")
        return ISSUANCE_FAILED

    ee_ocsp_url = "http://localhost:4002"

    # As OCSP-Updater is generating responses independently of the CA we sit in a loop
    # checking OCSP until we either see a good response or we timeout (5s).
    wait_for_ocsp_good(cert_file_pem, "test/test-ca2.pem", ee_ocsp_url)

    verify_ct_submission(expected_ct_submissions, "http://localhost:4500/submissions")

    # Check that the expiration mailer sends a reminder
    expiry = get_expiry_time(cert_file_pem)
    no_reminder = expiry + datetime.timedelta(days=-31)
    first_reminder = expiry + datetime.timedelta(days=-13)
    last_reminder = expiry + datetime.timedelta(days=-2)
    try:
        urllib2.urlopen("http://localhost:9381/clear", data='')
        get_future_output('./bin/expiration-mailer --config %s/expiration-mailer.json' %
            default_config_dir, no_reminder)
        get_future_output('./bin/expiration-mailer --config %s/expiration-mailer.json' %
            default_config_dir, first_reminder)
        get_future_output('./bin/expiration-mailer --config %s/expiration-mailer.json' %
            default_config_dir, last_reminder)
        resp = urllib2.urlopen("http://localhost:9381/count?to=%s" % email_addr)
        mailcount = int(resp.read())
        if mailcount != 2:
            print("\nExpiry mailer failed: expected 2 emails, got %d" % mailcount)
            return MAILER_FAILED
    except Exception as e:
        print("\nExpiry mailer failed:")
        print(e)
        return MAILER_FAILED

    if subprocess.Popen('''
        node revoke.js %s %s http://localhost:4000/acme/revoke-cert
        ''' % (cert_file, key_file), shell=True, cwd=JS_DIR).wait() != 0:
        print("\nRevoking failed")
        return REVOCATION_FAILED

    wait_for_ocsp_revoked(cert_file_pem, "test/test-ca2.pem", ee_ocsp_url)
    return 0

def run_custom(cmd, cwd=None):
    if subprocess.Popen(cmd, shell=True, cwd=cwd, executable='/bin/bash').wait() != 0:
        die(ExitStatus.PythonFailure)

def run_client_tests():
    root = os.environ.get("CERTBOT_PATH")
    assert root is not None, (
        "Please set CERTBOT_PATH env variable to point at "
        "initialized (virtualenv) client repo root")
    cmd = os.path.join(root, 'tests', 'boulder-integration.sh')
    run_custom(cmd, cwd=root)

# Run the single-ocsp command, which is used to generate OCSP responses for
# intermediate certificates on a manual basis.
def single_ocsp_sign():
    try:
        subprocess.check_output("""./bin/single-ocsp -issuer test/test-root.pem \
                    -responder test/test-root.pem \
                    -target test/test-ca2.pem \
                    -pkcs11 test/test-root.key-pkcs11.json \
                    -thisUpdate 2016-09-02T00:00:00Z \
                    -nextUpdate 2020-09-02T00:00:00Z \
                    -status 0 \
                    -out /tmp/issuer-ocsp-responses.txt""", shell=True)
    except subprocess.CalledProcessError as e:
        print("\nFailed to run single-ocsp: %s" % e)
        die(ExitStatus.PythonFailure)

    p = subprocess.Popen(
        './bin/ocsp-responder --config test/issuer-ocsp-responder.json', shell=True)

    global ocsp_proc
    ocsp_proc = p

    # Verify that the static OCSP responder, which answers with a
    # pre-signed, long-lived response for the CA cert, works.
    wait_for_ocsp_good("test/test-ca2.pem", "test/test-root.pem", "http://localhost:4003")

def get_future_output(cmd, date, cwd=None):
    return subprocess.check_output(cmd, cwd=cwd, env={'FAKECLOCK': date.strftime("%a %b %d %H:%M:%S UTC %Y")}, shell=True)

def run_expired_authz_purger_test():
    subprocess.check_output('''node test.js --email %s --domains %s --abort-step %s''' %
                            ("purger@test.com", "eap-test.com", "startChallenge"),
                            shell=True, cwd=JS_DIR)

    def expect(target_time, num):
        expected_output = 'Deleted a total of %d expired pending authorizations' % num
        try:
            out = get_future_output("./bin/expired-authz-purger --config cmd/expired-authz-purger/config.json --yes", target_time)
            if expected_output not in out:
                print("\nOutput from expired-authz-purger did not contain '%s'. Actual: %s"
                    % (expected_output, out))
                die(ExitStatus.NodeFailure)
        except subprocess.CalledProcessError as e:
            print("\nFailed to run authz purger: %s" % e)
            die(ExitStatus.NodeFailure)

    now = datetime.datetime.utcnow()
    after_grace_period = now + datetime.timedelta(days=+14, minutes=+3)
    expect(now, 0)
    expect(after_grace_period, 1)

def run_certificates_per_name_test():
    try:
        # This command will return a non zero error code. In order
        # to avoid a CalledProcessException we use Popen.
        handle = subprocess.Popen(
            '''node test.js --email %s --domains %s''' % ('test@lim.it', 'lim.it'),
            shell=True, cwd=JS_DIR, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        handle.wait()
        out, err = handle.communicate()
    except subprocess.CalledProcessError as e:
        print("\nFailure while running certificates per name test %s" % e)
        die(ExitStatus.PythonFailure)

    expected = [
        "urn:acme:error:rateLimited",
        "Error creating new cert :: Too many certificates already issued for: lim.it",
        "429"
    ]
    for s in expected:
        if s not in out:
            print("\nCertificates per name test: expected %s not present in output" % s)
            die(ExitStatus.Error)

default_config_dir = os.environ.get('BOULDER_CONFIG_DIR', '')
if default_config_dir == '':
    default_config_dir = 'test/config'

def run_admin_revoker_test():
    cert_file = os.path.join(tempdir, "ar-cert.der")
    cert_file_pem = os.path.join(tempdir, "ar-cert.pem")
    # Issue certificate for serial-revoke test
    if subprocess.Popen('''
        node test.js --domains ar-test.com --cert %s && \
        openssl x509 -in %s -out %s -inform der -outform pem
        ''' % (cert_file, cert_file, cert_file_pem),
        shell=True, cwd=JS_DIR).wait() != 0:
        print("\nIssuing failed")
        die(ExitStatus.NodeFailure)
    # Extract serial from certificate
    try:
        serial = subprocess.check_output("openssl x509 -in %s -noout -serial | cut -c 8-" % (cert_file_pem),
                                         shell=True, stderr=subprocess.STDOUT)
    except subprocess.CalledProcessError as e:
        print "Failed to extract serial: %s" % (e.output)
        die(ExitStatus.PythonFailure)
    serial = serial.rstrip()
    # Revoke certificate by serial
    config = default_config_dir + "/admin-revoker.json"
    if subprocess.Popen("./bin/admin-revoker serial-revoke --config %s %s %d" % (config, serial, 1),
                        shell=True).wait() != 0:
        print("Failed to revoke certificate")
        die(ExitStatus.RevokerFailure)
    # Wait for OCSP response to indicate revocation took place
    ee_ocsp_url = "http://localhost:4002"
    wait_for_ocsp_revoked(cert_file_pem, "test/test-ca2.pem", ee_ocsp_url)

    # Issue certificate for auth-revoke test
    try:
        output = subprocess.check_output("node test.js --domains ar-auth-test.com --abort-step startChallenge",
                            shell=True, cwd=JS_DIR)
    except subprocess.CalledProcessError as e:
        print "Failed to create authorization: %s" % (e.output)
        die(ExitStatus.NodeFailure)
    # Get authorization URL from last line of output
    lines = output.rstrip().split("\n")
    prefix = "authorization-url="
    if not lines[-1].startswith(prefix):
        print("Failed to extract authorization URL")
        die(ExitStatus.NodeFailure)
    url = lines[-1][len(prefix):]
    # Revoke authorization by domain
    try:
        output = subprocess.check_output("./bin/admin-revoker auth-revoke --config %s ar-auth-test.com" % (config),
                                         shell=True)
    except subprocess.CalledProcessError as e:
        print("Failed to revoke authorization: %s", e)
        die(ExitStatus.RevokerFailure)
    if not output.rstrip().endswith("Revoked 1 pending authorizations and 0 final authorizations"):
        print("admin-revoker didn't revoke the expected number of pending and finalized authorizations")
        die(ExitStatus.RevokerFailure)
    # Check authorization has actually been revoked
    response = urllib.urlopen(url)
    data = json.loads(response.read())
    if data['status'] != "revoked":
        print("Authorization wasn't revoked")
        die(ExitStatus.RevokerFailure)

exit_status = None
tempdir = tempfile.mkdtemp()

def main():
    parser = argparse.ArgumentParser(description='Run integration tests')
    parser.add_argument('--all', dest="run_all", action="store_true",
                        help="run all of the clients' integration tests")
    parser.add_argument('--certbot', dest='run_certbot', action='store_true',
                        help="run the certbot integration tests")
    parser.add_argument('--node', dest="run_node", action="store_true",
                        help="run the node client's integration tests")
    # allow any ACME client to run custom command for integration
    # testing (without having to implement its own busy-wait loop)
    parser.add_argument('--custom', metavar="CMD", help="run custom command")
    parser.set_defaults(run_all=False, run_certbot=False, run_node=False)
    args = parser.parse_args()

    if not (args.run_all or args.run_certbot or args.run_node or args.custom is not None):
        print >> sys.stderr, "must run at least one of the letsencrypt or node tests with --all, --certbot, --node, or --custom"
        die(ExitStatus.IncorrectCommandLineArgs)

    if not startservers.start(race_detection=True):
        die(ExitStatus.Error)

    single_ocsp_sign()

    if args.run_all or args.run_node:
        if subprocess.Popen('npm install', shell=True, cwd=JS_DIR).wait() != 0:
            print("\n Installing NPM modules failed")
            die(ExitStatus.Error)
        # Pick a random hostname so we don't run into certificate rate limiting.
        domain = "www." + subprocess.check_output("openssl rand -hex 6", shell=True).strip() + "-TEST.com"
        challenge_types = ["http-01", "dns-01"]

        expected_ct_submissions = 1
        resp = urllib2.urlopen("http://localhost:4500/submissions")
        submissionStr = resp.read()
        if int(submissionStr) > 0:
            expected_ct_submissions = int(submissionStr)+1
        for chall_type in challenge_types:
            if run_node_test(domain, chall_type, expected_ct_submissions) != 0:
                die(ExitStatus.NodeFailure)
            expected_ct_submissions += 1

        if run_node_test("good-caa-reserved.com", challenge_types[0], expected_ct_submissions) != 0:
            print("\nDidn't issue certificate for domain with good CAA records")
            die(ExitStatus.NodeFailure)

        if run_node_test("bad-caa-reserved.com", challenge_types[0], expected_ct_submissions) != ISSUANCE_FAILED:
            print("\nIssued certificate for domain with bad CAA records")
            die(ExitStatus.NodeFailure)

        run_expired_authz_purger_test()

        run_certificates_per_name_test()

        run_admin_revoker_test()

    # Simulate a disconnection from RabbitMQ to make sure reconnects work.
    startservers.bounce_forward()

    if args.run_all or args.run_certbot:
        run_client_tests()

    if args.custom:
        run_custom(args.custom)

    if not startservers.check():
        die(ExitStatus.Error)
    exit_status = ExitStatus.OK

if __name__ == "__main__":
    try:
        main()
    except Exception:
        exit_status = ExitStatus.Error
        raise

@atexit.register
def stop():
    import shutil
    shutil.rmtree(tempdir)
    if exit_status == ExitStatus.OK:
        print("\n\nSUCCESS")
    else:
        if exit_status:
            print("\n\nFAILURE %d" % exit_status)
    if ocsp_proc.poll() is None:
        ocsp_proc.kill()
