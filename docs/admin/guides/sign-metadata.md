# Sign Metadata

Administrators can add signing services to Pulp using the command line tools.
Users may then associate the signing services with repositories that support content signing.
The example below demonstrates how a signing service can be created using `gpg`:

## 1. Initial Setup

Make sure the service user `pulp` has access to `gpg` and that the keypair is installed in its keyrings.

The private key might alternatively be provided by a hardware cryptographic device.

## 2. Create signing script

Create a signing script that accepts a file name as the only argument.

The script needs to generate an ascii-armored detached GPG signature for that file,
using the key specified via the `PULP_SIGNING_KEY_FINGERPRINT` environment variable.
There is also a `CORRELATION_ID` environment variable available for logging purposes.
The script should then print out a JSON structure with the following format.
All the file names are relative paths inside the current working directory:

```
{"file": "filename", "signature": "filename.asc"}
```

The filename must remain the same for the detached signature, as shown.

!!! note

    This is specific to the `AsciiArmoredDetachedSigningService`.
    Plugins may provide other signing service classes that may need their JSON output to contain different information.

Below is an example of a signing script that produces signatures for content:

```bash
#!/usr/bin/env bash

FILE_PATH=$1
SIGNATURE_PATH="$1.asc"

ADMIN_ID="$PULP_SIGNING_KEY_FINGERPRINT"
PASSWORD="password"

# Create a detached signature
gpg --quiet --batch --pinentry-mode loopback --yes --passphrase \
   $PASSWORD --homedir ~/.gnupg/ --detach-sign --default-key $ADMIN_ID \
   --armor --output $SIGNATURE_PATH $FILE_PATH

# Check the exit status
STATUS=$?
if [ $STATUS -eq 0 ]; then
   echo {\"file\": \"$FILE_PATH\", \"signature\": \"$SIGNATURE_PATH\"}
else
   exit $STATUS
fi
```

!!! note

    As the creator of the signing script, you can expect `PULP_SIGNING_KEY_FINGERPRINT`,
    and potentially other environment variables,
    depending on the content plugin calling signing service.
    Make sure the script contains a proper shebang and Pulp has got valid permissions to execute it.
    If deploying in a distributed way, all `pulpcore-worker` processes need to be able to access this script in the same path.

## 3. Create a signing service

Create a signing service consisting of an absolute path to the script,
a meaningful name describing the script's purpose, and the identity identifying the key for signing.

The script must be executable.
Here is an example showing how to create one instance of a signing service:

```bash
pulpcore-manager add-signing-service ${SERVICE_NAME} ${SCRIPT_ABS_FILENAME} ${KEYID}
```

!!! note

    The public key must be available on the caller's keyring or on a keyring provided via the `--gpghome` or `--keyring` parameters.

## 4. Check the signing service

Retrieve and check the saved signing service via REST API:

=== "run"

      ```bash
      pulp signing-service list
      ```

=== "output"

      ```json
      [
          {
              "name": "sign-metadata",
              "pubkey_fingerprint": "19CD52BD1CA9A00DF10A842D74B14E3590C2231F",
              "public_key": "-----BEGIN PGP PUBLIC KEY BLOCK-----\n\n [...] \n-----END PGP PUBLIC KEY BLOCK-----\n",
              "pulp_created": "2020-11-06T15:42:20.645197Z",
              "pulp_href": "/pulp/api/v3/signing-services/ffb9e987-952f-47e3-a274-ffe69a80ded7/",
              "prn": "prn:core.signingservice:ffb9e987-952f-47e3-a274-ffe69a80ded7",
              "script": "/var/lib/pulp/sign-metadata.sh"
          }
      ]
      ```

Plugins referring to this signing service can now sign selected content by the provided script.
However support and details of this process varies greatly between different content types.
To learn more about the signing from a plugin writer's perspective, see the section [Metadata Signing].

[Metadata Signing]: site:pulpcore/docs/dev/learn/other/metadata-signing/
