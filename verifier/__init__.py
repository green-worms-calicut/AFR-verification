"""The verifier service gw_sales calls for every new or edited disposal log.

gw_sales sends a signed request (POST /v1/verifications); the service answers 202, checks the
disposal's weigh bills, collection photos and unloading photos with check_proof, and posts its
flags back to the request's callbackUrl, as the Disposal Verification Contract (schema 1) sets out.
"""
