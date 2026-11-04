---
# Variables consumed by the template ({{component}}, {{reference_urls}}, {{attachments}})
component: "Amazon S3 Files"
business_unit: "Aegon Spain"
reference_urls:
  - https://docs.aws.amazon.com/AmazonS3/latest/userguide/s3-files.html
  - https://docs.aws.amazon.com/AmazonS3/latest/userguide/s3-files-quotas.html
attachments: []
---
GTS Architecture Delivery
Describe the issue and what kind of guidance or outcome you're looking for. : We want to replace our current Amazon S3 bucket access method (AWS Storage Gateway) with a simpler, native approach. We initially planned to use s3fs-fuse, but it is only available for Amazon Linux 2 (AL2) and not for Amazon Linux 2023 (AL2023). On AL2023 we tested Mountpoint for Amazon S3 (mount-s3), but it does not support file move operations (rename/move semantics), which is required by our workloads. Therefore, we are requesting ET approval and enablement to use Amazon S3 Files as described in the AWS documentation: https://docs.aws.amazon.com/AmazonS3/latest/userguide/s3-files.html 
Outcome requested: confirmation that S3 Files is an approved/supported approach for our BU, plus architecture/security guidance (pattern, controls, and any prerequisites) so we can decommission Storage Gateway instead of upgrading it.
What is the business need or problem you are trying to solve? : We need a supported and maintainable way to expose S3 object storage as a file interface on AL2023 systems while preserving required file operations (including move/rename). Our current Storage Gateway solution works but introduces operational overhead and upgrade dependency. If we cannot adopt S3 Files, we will be forced to resume/plan a Storage Gateway upgrade with ET, which we were trying to avoid.
List any deadlines, budget constraints, compliance needs, or technical restrictions. : Deadlines: Not formally defined yet; we would like a timely decision to avoid rework and to prevent having to restart a Storage Gateway upgrade track unnecessarily, which has end of support end of July
Budget constraints: Prefer a solution that minimizes additional infrastructure and operational cost compared to maintaining Storage Gateway.
Technical restrictions: Target OS is Amazon Linux 2023 (AL2023). s3fs-fuse is not available for AL2023; mount-s3 lacks required move/rename behavior for our use case.
Explain the current state process, business and IT architecture landscape. : Current state: We provide file-style access to data stored in an S3 bucket through AWS Storage Gateway, effectively exposing the bucket as a mounted file share to our compute workloads.
Target state: Replace Storage Gateway with a supported alternative on AL2023, ideally Amazon S3 Files, to maintain the same functional behavior while reducing operational complexity and avoiding Storage Gateway upgrade dependency
What Business Units are impacted by this request? : Aegon Spain
What business requirements must be considered for addressing the architecture consulting or PoC need? : Must preserve required application/file workflows that depend on file creation/remove/move/rename behavior.
Must run on AL2023 (standardized OS baseline).
Must follow the ET engagement model and ensure the appropriate risk/security/privacy considerations are addressed for the chosen approach.
What specific features or capabilities are needed? : A supported "files" interface for S3 that can be used from AL2023 workloads.
Ability to perform standard file operations required by our applications, specifically including move/rename semantics (which currently blocks us with mount-s3). Also create and remove operations.
Clear ET position on supportability, required controls, and any approved patterns for implementation.
Include performance, security, scalability, or other quality expectations. : Better performance than the current Storage Gateway-based user experience for day-to-day operations (latency/throughput acceptable for file workflows).
Describe the type, source, and volume of data involved. : Type/source: Existing data already stored in an Amazon S3 bucket and currently accessed via Storage Gateway as a mounted share.
Volume: GB/TB
List the criteria or metrics that define a successful outcome. : Ability to use a supported solution on AL2023 to access S3 data via a file interface.
Required file operations (including move/rename) work as needed by the workload.
Security/compliance expectations satisfied (reviewed/approved as applicable).
We can decommission Storage Gateway for this use case (or minimize its scope) instead of restarting a major upgrade effort.
Azure : false
AWS : true
On-Prem/Data Center : false
Co-location : false
SaaS : false
Does this consulting request or PoC involve introduction or use of new Generative AI (GenAI) capabilities? If yes, provide the GenAI Inventory ID and date of working group review. : No. This request is strictly about storage/file access architecture and does not introduce or use GenAI capabilities.
What are the expected deliverables? Include documents, prototypes, recommendations. : A ET decision on whether Amazon S3 Files is approved/supported for our BU and for this use case.
Recommended reference architecture / pattern and required controls (security, logging, monitoring, operational requirements).
List key stakeholders. : Jesús Gomez, Samuel Moreno, Francisco Gragera, Edgar Ramos, Aaron de la Paz
If the PoC moves to a project implementation, who will be the executive sponsor accountable for funding the initiative? : Edgar Ramos
