"""Internal JSON job transport. Preprocessing runs in its own container."""
import base64
from common.service_client import ServiceClient


class ProcessingServiceClient(ServiceClient):
    def process(self, job, file, identity, *, guard):
        payload = {"identity": identity, "job": job.preprocessing, "context": job.context,
                   "file": {"filename": file.filename, "data_base64": base64.b64encode(file.data).decode("ascii"),
                            "metadata": file.metadata}}
        result = self.run_job(payload, guard=guard)
        contract = result.get("contract")
        if (not isinstance(contract, dict) or contract.get("contract_id") != job.contract_id
                or str(contract.get("contract_version")) != job.contract_version):
            raise ValueError("Preprocessing returned another contract or contract_version")
        return contract
