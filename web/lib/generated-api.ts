/* Generated from the backend OpenAPI document. Do not edit. */
export interface paths {
    readonly "/api/v1/candidates": {
        readonly parameters: {
            readonly query?: never;
            readonly header?: never;
            readonly path?: never;
            readonly cookie?: never;
        };
        /** Directory */
        readonly get: operations["directory_api_v1_candidates_get"];
        readonly put?: never;
        readonly post?: never;
        readonly delete?: never;
        readonly options?: never;
        readonly head?: never;
        readonly patch?: never;
        readonly trace?: never;
    };
    readonly "/api/v1/candidates/{candidate_id}/jobs": {
        readonly parameters: {
            readonly query?: never;
            readonly header?: never;
            readonly path?: never;
            readonly cookie?: never;
        };
        /** Candidate Jobs */
        readonly get: operations["candidate_jobs_api_v1_candidates__candidate_id__jobs_get"];
        readonly put?: never;
        readonly post?: never;
        readonly delete?: never;
        readonly options?: never;
        readonly head?: never;
        readonly patch?: never;
        readonly trace?: never;
    };
    readonly "/api/v1/clients": {
        readonly parameters: {
            readonly query?: never;
            readonly header?: never;
            readonly path?: never;
            readonly cookie?: never;
        };
        /** List Clients */
        readonly get: operations["list_clients_api_v1_clients_get"];
        readonly put?: never;
        /** Create Client */
        readonly post: operations["create_client_api_v1_clients_post"];
        readonly delete?: never;
        readonly options?: never;
        readonly head?: never;
        readonly patch?: never;
        readonly trace?: never;
    };
    readonly "/api/v1/clients/{client_id}": {
        readonly parameters: {
            readonly query?: never;
            readonly header?: never;
            readonly path?: never;
            readonly cookie?: never;
        };
        /** Get Client */
        readonly get: operations["get_client_api_v1_clients__client_id__get"];
        readonly put?: never;
        readonly post?: never;
        readonly delete?: never;
        readonly options?: never;
        readonly head?: never;
        readonly patch?: never;
        readonly trace?: never;
    };
    readonly "/api/v1/clients/{client_id}/adjacent-industries": {
        readonly parameters: {
            readonly query?: never;
            readonly header?: never;
            readonly path?: never;
            readonly cookie?: never;
        };
        readonly get?: never;
        /** Approve Client Adjacency */
        readonly put: operations["approve_client_adjacency_api_v1_clients__client_id__adjacent_industries_put"];
        readonly post?: never;
        readonly delete?: never;
        readonly options?: never;
        readonly head?: never;
        readonly patch?: never;
        readonly trace?: never;
    };
    readonly "/api/v1/clients/{client_id}/grants": {
        readonly parameters: {
            readonly query?: never;
            readonly header?: never;
            readonly path?: never;
            readonly cookie?: never;
        };
        readonly get?: never;
        readonly put?: never;
        /** Grant Client Access */
        readonly post: operations["grant_client_access_api_v1_clients__client_id__grants_post"];
        readonly delete?: never;
        readonly options?: never;
        readonly head?: never;
        readonly patch?: never;
        readonly trace?: never;
    };
    readonly "/api/v1/clients/{client_id}/industries": {
        readonly parameters: {
            readonly query?: never;
            readonly header?: never;
            readonly path?: never;
            readonly cookie?: never;
        };
        readonly get?: never;
        /** Update Client Industries */
        readonly put: operations["update_client_industries_api_v1_clients__client_id__industries_put"];
        readonly post?: never;
        readonly delete?: never;
        readonly options?: never;
        readonly head?: never;
        readonly patch?: never;
        readonly trace?: never;
    };
    readonly "/api/v1/contact-points/{contact_point_id}/reveal": {
        readonly parameters: {
            readonly query?: never;
            readonly header?: never;
            readonly path?: never;
            readonly cookie?: never;
        };
        readonly get?: never;
        readonly put?: never;
        /** Reveal Contact */
        readonly post: operations["reveal_contact_api_v1_contact_points__contact_point_id__reveal_post"];
        readonly delete?: never;
        readonly options?: never;
        readonly head?: never;
        readonly patch?: never;
        readonly trace?: never;
    };
    readonly "/api/v1/job-candidates/{job_candidate_id}": {
        readonly parameters: {
            readonly query?: never;
            readonly header?: never;
            readonly path?: never;
            readonly cookie?: never;
        };
        /** Get Job Candidate */
        readonly get: operations["get_job_candidate_api_v1_job_candidates__job_candidate_id__get"];
        readonly put?: never;
        readonly post?: never;
        readonly delete?: never;
        readonly options?: never;
        readonly head?: never;
        readonly patch?: never;
        readonly trace?: never;
    };
    readonly "/api/v1/job-candidates/{job_candidate_id}/activity": {
        readonly parameters: {
            readonly query?: never;
            readonly header?: never;
            readonly path?: never;
            readonly cookie?: never;
        };
        /** List Activity */
        readonly get: operations["list_activity_api_v1_job_candidates__job_candidate_id__activity_get"];
        readonly put?: never;
        readonly post?: never;
        readonly delete?: never;
        readonly options?: never;
        readonly head?: never;
        readonly patch?: never;
        readonly trace?: never;
    };
    readonly "/api/v1/job-candidates/{job_candidate_id}/notes": {
        readonly parameters: {
            readonly query?: never;
            readonly header?: never;
            readonly path?: never;
            readonly cookie?: never;
        };
        readonly get?: never;
        readonly put?: never;
        /** Add Note */
        readonly post: operations["add_note_api_v1_job_candidates__job_candidate_id__notes_post"];
        readonly delete?: never;
        readonly options?: never;
        readonly head?: never;
        readonly patch?: never;
        readonly trace?: never;
    };
    readonly "/api/v1/job-candidates/{job_candidate_id}/owner": {
        readonly parameters: {
            readonly query?: never;
            readonly header?: never;
            readonly path?: never;
            readonly cookie?: never;
        };
        readonly get?: never;
        readonly put?: never;
        readonly post?: never;
        readonly delete?: never;
        readonly options?: never;
        readonly head?: never;
        /** Update Owner */
        readonly patch: operations["update_owner_api_v1_job_candidates__job_candidate_id__owner_patch"];
        readonly trace?: never;
    };
    readonly "/api/v1/job-candidates/{job_candidate_id}/stage": {
        readonly parameters: {
            readonly query?: never;
            readonly header?: never;
            readonly path?: never;
            readonly cookie?: never;
        };
        readonly get?: never;
        readonly put?: never;
        readonly post?: never;
        readonly delete?: never;
        readonly options?: never;
        readonly head?: never;
        /** Update Stage */
        readonly patch: operations["update_stage_api_v1_job_candidates__job_candidate_id__stage_patch"];
        readonly trace?: never;
    };
    readonly "/api/v1/job-candidates/{job_candidate_id}/tags": {
        readonly parameters: {
            readonly query?: never;
            readonly header?: never;
            readonly path?: never;
            readonly cookie?: never;
        };
        readonly get?: never;
        /** Update Tags */
        readonly put: operations["update_tags_api_v1_job_candidates__job_candidate_id__tags_put"];
        readonly post?: never;
        readonly delete?: never;
        readonly options?: never;
        readonly head?: never;
        readonly patch?: never;
        readonly trace?: never;
    };
    readonly "/api/v1/job-candidates/{run_candidate_id}/enrich": {
        readonly parameters: {
            readonly query?: never;
            readonly header?: never;
            readonly path?: never;
            readonly cookie?: never;
        };
        readonly get?: never;
        readonly put?: never;
        /** Request Candidate Enrichment */
        readonly post: operations["request_candidate_enrichment_api_v1_job_candidates__run_candidate_id__enrich_post"];
        readonly delete?: never;
        readonly options?: never;
        readonly head?: never;
        readonly patch?: never;
        readonly trace?: never;
    };
    readonly "/api/v1/jobs": {
        readonly parameters: {
            readonly query?: never;
            readonly header?: never;
            readonly path?: never;
            readonly cookie?: never;
        };
        /** List Jobs */
        readonly get: operations["list_jobs_api_v1_jobs_get"];
        readonly put?: never;
        /** Create Job */
        readonly post: operations["create_job_api_v1_jobs_post"];
        readonly delete?: never;
        readonly options?: never;
        readonly head?: never;
        readonly patch?: never;
        readonly trace?: never;
    };
    readonly "/api/v1/jobs/{job_id}": {
        readonly parameters: {
            readonly query?: never;
            readonly header?: never;
            readonly path?: never;
            readonly cookie?: never;
        };
        /** Get Job */
        readonly get: operations["get_job_api_v1_jobs__job_id__get"];
        readonly put?: never;
        readonly post?: never;
        readonly delete?: never;
        readonly options?: never;
        readonly head?: never;
        readonly patch?: never;
        readonly trace?: never;
    };
    readonly "/api/v1/jobs/{job_id}/acceptance": {
        readonly parameters: {
            readonly query?: never;
            readonly header?: never;
            readonly path?: never;
            readonly cookie?: never;
        };
        /** Acceptance */
        readonly get: operations["acceptance_api_v1_jobs__job_id__acceptance_get"];
        readonly put?: never;
        readonly post?: never;
        readonly delete?: never;
        readonly options?: never;
        readonly head?: never;
        readonly patch?: never;
        readonly trace?: never;
    };
    readonly "/api/v1/jobs/{job_id}/candidates": {
        readonly parameters: {
            readonly query?: never;
            readonly header?: never;
            readonly path?: never;
            readonly cookie?: never;
        };
        /** List Job Candidates */
        readonly get: operations["list_job_candidates_api_v1_jobs__job_id__candidates_get"];
        readonly put?: never;
        readonly post?: never;
        readonly delete?: never;
        readonly options?: never;
        readonly head?: never;
        readonly patch?: never;
        readonly trace?: never;
    };
    readonly "/api/v1/jobs/{job_id}/export.csv": {
        readonly parameters: {
            readonly query?: never;
            readonly header?: never;
            readonly path?: never;
            readonly cookie?: never;
        };
        /** Export Shortlist */
        readonly get: operations["export_shortlist_api_v1_jobs__job_id__export_csv_get"];
        readonly put?: never;
        readonly post?: never;
        readonly delete?: never;
        readonly options?: never;
        readonly head?: never;
        readonly patch?: never;
        readonly trace?: never;
    };
    readonly "/api/v1/jobs/{job_id}/rescore": {
        readonly parameters: {
            readonly query?: never;
            readonly header?: never;
            readonly path?: never;
            readonly cookie?: never;
        };
        readonly get?: never;
        readonly put?: never;
        /** Rescore Job */
        readonly post: operations["rescore_job_api_v1_jobs__job_id__rescore_post"];
        readonly delete?: never;
        readonly options?: never;
        readonly head?: never;
        readonly patch?: never;
        readonly trace?: never;
    };
    readonly "/api/v1/jobs/{job_id}/runs": {
        readonly parameters: {
            readonly query?: never;
            readonly header?: never;
            readonly path?: never;
            readonly cookie?: never;
        };
        readonly get?: never;
        readonly put?: never;
        /** Start Run */
        readonly post: operations["start_run_api_v1_jobs__job_id__runs_post"];
        readonly delete?: never;
        readonly options?: never;
        readonly head?: never;
        readonly patch?: never;
        readonly trace?: never;
    };
    readonly "/api/v1/jobs/{job_id}/scorecard/confirm": {
        readonly parameters: {
            readonly query?: never;
            readonly header?: never;
            readonly path?: never;
            readonly cookie?: never;
        };
        readonly get?: never;
        readonly put?: never;
        /** Confirm Scorecard */
        readonly post: operations["confirm_scorecard_api_v1_jobs__job_id__scorecard_confirm_post"];
        readonly delete?: never;
        readonly options?: never;
        readonly head?: never;
        readonly patch?: never;
        readonly trace?: never;
    };
    readonly "/api/v1/jobs/{job_id}/scorecard/draft": {
        readonly parameters: {
            readonly query?: never;
            readonly header?: never;
            readonly path?: never;
            readonly cookie?: never;
        };
        /** Get Scorecard Draft */
        readonly get: operations["get_scorecard_draft_api_v1_jobs__job_id__scorecard_draft_get"];
        /** Update Scorecard Draft */
        readonly put: operations["update_scorecard_draft_api_v1_jobs__job_id__scorecard_draft_put"];
        readonly post?: never;
        readonly delete?: never;
        readonly options?: never;
        readonly head?: never;
        readonly patch?: never;
        readonly trace?: never;
    };
    readonly "/api/v1/jobs/{job_id}/scorecard/generate": {
        readonly parameters: {
            readonly query?: never;
            readonly header?: never;
            readonly path?: never;
            readonly cookie?: never;
        };
        readonly get?: never;
        readonly put?: never;
        /** Generate Scorecard Draft */
        readonly post: operations["generate_scorecard_draft_api_v1_jobs__job_id__scorecard_generate_post"];
        readonly delete?: never;
        readonly options?: never;
        readonly head?: never;
        readonly patch?: never;
        readonly trace?: never;
    };
    readonly "/api/v1/jobs/{job_id}/scorecards": {
        readonly parameters: {
            readonly query?: never;
            readonly header?: never;
            readonly path?: never;
            readonly cookie?: never;
        };
        /** List Scorecard Versions */
        readonly get: operations["list_scorecard_versions_api_v1_jobs__job_id__scorecards_get"];
        readonly put?: never;
        readonly post?: never;
        readonly delete?: never;
        readonly options?: never;
        readonly head?: never;
        readonly patch?: never;
        readonly trace?: never;
    };
    readonly "/api/v1/me": {
        readonly parameters: {
            readonly query?: never;
            readonly header?: never;
            readonly path?: never;
            readonly cookie?: never;
        };
        /** Me */
        readonly get: operations["me_api_v1_me_get"];
        readonly put?: never;
        readonly post?: never;
        readonly delete?: never;
        readonly options?: never;
        readonly head?: never;
        readonly patch?: never;
        readonly trace?: never;
    };
    readonly "/api/v1/members": {
        readonly parameters: {
            readonly query?: never;
            readonly header?: never;
            readonly path?: never;
            readonly cookie?: never;
        };
        /** List Members */
        readonly get: operations["list_members_api_v1_members_get"];
        readonly put?: never;
        readonly post?: never;
        readonly delete?: never;
        readonly options?: never;
        readonly head?: never;
        readonly patch?: never;
        readonly trace?: never;
    };
    readonly "/api/v1/members/{membership_id}": {
        readonly parameters: {
            readonly query?: never;
            readonly header?: never;
            readonly path?: never;
            readonly cookie?: never;
        };
        readonly get?: never;
        readonly put?: never;
        readonly post?: never;
        /** Deactivate Member */
        readonly delete: operations["deactivate_member_api_v1_members__membership_id__delete"];
        readonly options?: never;
        readonly head?: never;
        readonly patch?: never;
        readonly trace?: never;
    };
    readonly "/api/v1/members/{membership_id}/role": {
        readonly parameters: {
            readonly query?: never;
            readonly header?: never;
            readonly path?: never;
            readonly cookie?: never;
        };
        readonly get?: never;
        readonly put?: never;
        readonly post?: never;
        readonly delete?: never;
        readonly options?: never;
        readonly head?: never;
        /** Change Member Role */
        readonly patch: operations["change_member_role_api_v1_members__membership_id__role_patch"];
        readonly trace?: never;
    };
    readonly "/api/v1/membership-invitations": {
        readonly parameters: {
            readonly query?: never;
            readonly header?: never;
            readonly path?: never;
            readonly cookie?: never;
        };
        readonly get?: never;
        readonly put?: never;
        /** Create Invitation */
        readonly post: operations["create_invitation_api_v1_membership_invitations_post"];
        readonly delete?: never;
        readonly options?: never;
        readonly head?: never;
        readonly patch?: never;
        readonly trace?: never;
    };
    readonly "/api/v1/membership-invitations/{token}/claim": {
        readonly parameters: {
            readonly query?: never;
            readonly header?: never;
            readonly path?: never;
            readonly cookie?: never;
        };
        readonly get?: never;
        readonly put?: never;
        /** Claim Invitation */
        readonly post: operations["claim_invitation_api_v1_membership_invitations__token__claim_post"];
        readonly delete?: never;
        readonly options?: never;
        readonly head?: never;
        readonly patch?: never;
        readonly trace?: never;
    };
    readonly "/api/v1/notifications": {
        readonly parameters: {
            readonly query?: never;
            readonly header?: never;
            readonly path?: never;
            readonly cookie?: never;
        };
        /** List Notifications */
        readonly get: operations["list_notifications_api_v1_notifications_get"];
        readonly put?: never;
        readonly post?: never;
        readonly delete?: never;
        readonly options?: never;
        readonly head?: never;
        readonly patch?: never;
        readonly trace?: never;
    };
    readonly "/api/v1/notifications/{notification_id}": {
        readonly parameters: {
            readonly query?: never;
            readonly header?: never;
            readonly path?: never;
            readonly cookie?: never;
        };
        readonly get?: never;
        readonly put?: never;
        readonly post?: never;
        readonly delete?: never;
        readonly options?: never;
        readonly head?: never;
        /** Acknowledge Notification */
        readonly patch: operations["acknowledge_notification_api_v1_notifications__notification_id__patch"];
        readonly trace?: never;
    };
    readonly "/api/v1/privacy-requests": {
        readonly parameters: {
            readonly query?: never;
            readonly header?: never;
            readonly path?: never;
            readonly cookie?: never;
        };
        /** List Privacy Requests */
        readonly get: operations["list_privacy_requests_api_v1_privacy_requests_get"];
        readonly put?: never;
        /** Submit Privacy Request */
        readonly post: operations["submit_privacy_request_api_v1_privacy_requests_post"];
        readonly delete?: never;
        readonly options?: never;
        readonly head?: never;
        readonly patch?: never;
        readonly trace?: never;
    };
    readonly "/api/v1/privacy-requests/{request_id}": {
        readonly parameters: {
            readonly query?: never;
            readonly header?: never;
            readonly path?: never;
            readonly cookie?: never;
        };
        /** Privacy Request Status */
        readonly get: operations["privacy_request_status_api_v1_privacy_requests__request_id__get"];
        readonly put?: never;
        readonly post?: never;
        readonly delete?: never;
        readonly options?: never;
        readonly head?: never;
        readonly patch?: never;
        readonly trace?: never;
    };
    readonly "/api/v1/privacy-requests/{request_id}/approve": {
        readonly parameters: {
            readonly query?: never;
            readonly header?: never;
            readonly path?: never;
            readonly cookie?: never;
        };
        readonly get?: never;
        readonly put?: never;
        /** Approve Privacy Request */
        readonly post: operations["approve_privacy_request_api_v1_privacy_requests__request_id__approve_post"];
        readonly delete?: never;
        readonly options?: never;
        readonly head?: never;
        readonly patch?: never;
        readonly trace?: never;
    };
    readonly "/api/v1/privacy-requests/{request_id}/execute": {
        readonly parameters: {
            readonly query?: never;
            readonly header?: never;
            readonly path?: never;
            readonly cookie?: never;
        };
        readonly get?: never;
        readonly put?: never;
        /** Execute Privacy Request */
        readonly post: operations["execute_privacy_request_api_v1_privacy_requests__request_id__execute_post"];
        readonly delete?: never;
        readonly options?: never;
        readonly head?: never;
        readonly patch?: never;
        readonly trace?: never;
    };
    readonly "/api/v1/privacy-requests/{request_id}/reject": {
        readonly parameters: {
            readonly query?: never;
            readonly header?: never;
            readonly path?: never;
            readonly cookie?: never;
        };
        readonly get?: never;
        readonly put?: never;
        /** Reject Privacy Request */
        readonly post: operations["reject_privacy_request_api_v1_privacy_requests__request_id__reject_post"];
        readonly delete?: never;
        readonly options?: never;
        readonly head?: never;
        readonly patch?: never;
        readonly trace?: never;
    };
    readonly "/api/v1/privacy-requests/{request_id}/verify": {
        readonly parameters: {
            readonly query?: never;
            readonly header?: never;
            readonly path?: never;
            readonly cookie?: never;
        };
        readonly get?: never;
        readonly put?: never;
        /** Verify Privacy Request */
        readonly post: operations["verify_privacy_request_api_v1_privacy_requests__request_id__verify_post"];
        readonly delete?: never;
        readonly options?: never;
        readonly head?: never;
        readonly patch?: never;
        readonly trace?: never;
    };
    readonly "/api/v1/runs/{run_id}": {
        readonly parameters: {
            readonly query?: never;
            readonly header?: never;
            readonly path?: never;
            readonly cookie?: never;
        };
        /** Get Run */
        readonly get: operations["get_run_api_v1_runs__run_id__get"];
        readonly put?: never;
        readonly post?: never;
        readonly delete?: never;
        readonly options?: never;
        readonly head?: never;
        readonly patch?: never;
        readonly trace?: never;
    };
    readonly "/api/v1/runs/{run_id}/activity": {
        readonly parameters: {
            readonly query?: never;
            readonly header?: never;
            readonly path?: never;
            readonly cookie?: never;
        };
        /** Get Run Activity */
        readonly get: operations["get_run_activity_api_v1_runs__run_id__activity_get"];
        readonly put?: never;
        readonly post?: never;
        readonly delete?: never;
        readonly options?: never;
        readonly head?: never;
        readonly patch?: never;
        readonly trace?: never;
    };
    readonly "/api/v1/runs/{run_id}/cancel": {
        readonly parameters: {
            readonly query?: never;
            readonly header?: never;
            readonly path?: never;
            readonly cookie?: never;
        };
        readonly get?: never;
        readonly put?: never;
        /** Cancel Run */
        readonly post: operations["cancel_run_api_v1_runs__run_id__cancel_post"];
        readonly delete?: never;
        readonly options?: never;
        readonly head?: never;
        readonly patch?: never;
        readonly trace?: never;
    };
    readonly "/health/ready": {
        readonly parameters: {
            readonly query?: never;
            readonly header?: never;
            readonly path?: never;
            readonly cookie?: never;
        };
        /** Ready */
        readonly get: operations["ready_health_ready_get"];
        readonly put?: never;
        readonly post?: never;
        readonly delete?: never;
        readonly options?: never;
        readonly head?: never;
        readonly patch?: never;
        readonly trace?: never;
    };
    readonly "/webhooks/apollo/{capability_token}": {
        readonly parameters: {
            readonly query?: never;
            readonly header?: never;
            readonly path?: never;
            readonly cookie?: never;
        };
        readonly get?: never;
        readonly put?: never;
        /** Apollo Webhook */
        readonly post: operations["apollo_webhook_webhooks_apollo__capability_token__post"];
        readonly delete?: never;
        readonly options?: never;
        readonly head?: never;
        readonly patch?: never;
        readonly trace?: never;
    };
}
export type webhooks = Record<string, never>;
export interface components {
    schemas: {
        /** AcceptanceResponse */
        readonly AcceptanceResponse: {
            /** Accepted */
            readonly accepted: number;
            /** Denominator */
            readonly denominator: number;
            /** Final */
            readonly final: boolean;
            /**
             * Final At
             * Format: date-time
             */
            readonly final_at: string;
            /** New */
            readonly new: number;
            /** Rate */
            readonly rate: number;
            /**
             * Ready At
             * Format: date-time
             */
            readonly ready_at: string;
            /** Rejected */
            readonly rejected: number;
            /** Reviewed */
            readonly reviewed: number;
            /** Shortlisted */
            readonly shortlisted: number;
        };
        /** ActivityPage */
        readonly ActivityPage: {
            /** Items */
            readonly items: readonly components["schemas"]["ActivityResponse"][];
            /** Next Cursor */
            readonly next_cursor: string | null;
        };
        /** ActivityResponse */
        readonly ActivityResponse: {
            /** Action */
            readonly action: string;
            /**
             * Actor User Id
             * Format: uuid
             */
            readonly actor_user_id: string;
            /**
             * Created At
             * Format: date-time
             */
            readonly created_at: string;
            /**
             * Id
             * Format: uuid
             */
            readonly id: string;
            /**
             * Job Candidate Id
             * Format: uuid
             */
            readonly job_candidate_id: string;
            /** Payload */
            readonly payload: {
                readonly [key: string]: unknown;
            };
            /**
             * Updated At
             * Format: date-time
             */
            readonly updated_at: string;
        };
        /** CandidateDirectoryItem */
        readonly CandidateDirectoryItem: {
            /** Current Company */
            readonly current_company: string | null;
            /** Current Title */
            readonly current_title: string | null;
            /**
             * Id
             * Format: uuid
             */
            readonly id: string;
            /** Industry Codes */
            readonly industry_codes: readonly string[];
            /** Job Ids */
            readonly job_ids: readonly string[];
            /** Location */
            readonly location: string | null;
            /** Name */
            readonly name: string;
            /**
             * Updated At
             * Format: date-time
             */
            readonly updated_at: string;
        };
        /** CandidateDirectoryPage */
        readonly CandidateDirectoryPage: {
            /** Items */
            readonly items: readonly components["schemas"]["CandidateDirectoryItem"][];
            /** Next Cursor */
            readonly next_cursor: string | null;
        };
        /** CandidateJobView */
        readonly CandidateJobView: {
            /** Classification */
            readonly classification: string;
            /**
             * Job Candidate Id
             * Format: uuid
             */
            readonly job_candidate_id: string;
            /**
             * Job Id
             * Format: uuid
             */
            readonly job_id: string;
            /** Job Title */
            readonly job_title: string;
            /** Score */
            readonly score: number;
            readonly stage: components["schemas"]["CandidateStage"];
            /**
             * Updated At
             * Format: date-time
             */
            readonly updated_at: string;
        };
        /**
         * CandidateStage
         * @enum {string}
         */
        readonly CandidateStage: "New" | "Reviewed" | "Shortlisted" | "Rejected";
        /** ClientAdjacencyUpdate */
        readonly ClientAdjacencyUpdate: {
            /** Adjacent Industry Code */
            readonly adjacent_industry_code: string;
            /** Industry Code */
            readonly industry_code: string;
        };
        /** ClientCreate */
        readonly ClientCreate: {
            /** Industry Codes */
            readonly industry_codes?: readonly string[];
            /** Name */
            readonly name: string;
        };
        /** ClientGrantCreate */
        readonly ClientGrantCreate: {
            /**
             * Membership Id
             * Format: uuid
             */
            readonly membership_id: string;
        };
        /** ClientGrantResponse */
        readonly ClientGrantResponse: {
            /**
             * Client Id
             * Format: uuid
             */
            readonly client_id: string;
            /**
             * Membership Id
             * Format: uuid
             */
            readonly membership_id: string;
        };
        /** ClientIndustriesUpdate */
        readonly ClientIndustriesUpdate: {
            /** Industry Codes */
            readonly industry_codes: readonly string[];
        };
        /** ClientResponse */
        readonly ClientResponse: {
            /** Adjacent Industries */
            readonly adjacent_industries: readonly (readonly [
                string,
                string
            ])[];
            /**
             * Id
             * Format: uuid
             */
            readonly id: string;
            /** Industry Codes */
            readonly industry_codes: readonly string[];
            /** Name */
            readonly name: string;
            /**
             * Tenant Id
             * Format: uuid
             */
            readonly tenant_id: string;
        };
        /** ConfirmedScorecard */
        readonly ConfirmedScorecard: {
            /**
             * Confirmed At
             * Format: date-time
             */
            readonly confirmed_at: string;
            /** Confirmed Inferred Items */
            readonly confirmed_inferred_items?: readonly string[];
            /** Criteria */
            readonly criteria: readonly components["schemas"]["ScorecardCriterion"][];
            readonly extraction_status: components["schemas"]["ExtractionStatus"];
            /**
             * Id
             * Format: uuid
             */
            readonly id: string;
            /** Industry Code */
            readonly industry_code: string;
            /**
             * Job Id
             * Format: uuid
             */
            readonly job_id: string;
            /** Locations */
            readonly locations: readonly string[];
            /** Maximum Years */
            readonly maximum_years?: number | null;
            /** Minimum Years */
            readonly minimum_years?: number | null;
            /** Seniority */
            readonly seniority: readonly string[];
            /** Suggested Adjacent Industries */
            readonly suggested_adjacent_industries: readonly string[];
            /** Target Titles */
            readonly target_titles: readonly string[];
            /** Uncertainties */
            readonly uncertainties: readonly string[];
            /** Version */
            readonly version: number;
        };
        /** ContactRevealResponse */
        readonly ContactRevealResponse: {
            /**
             * Id
             * Format: uuid
             */
            readonly id: string;
            /** Value */
            readonly value: string;
        };
        /**
         * CriterionKind
         * @enum {string}
         */
        readonly CriterionKind: "must_have" | "preference" | "exclusion";
        /** EditableScorecardDraft */
        readonly EditableScorecardDraft: {
            /** Confirmed Inferred Items */
            readonly confirmed_inferred_items?: readonly string[];
            /** Criteria */
            readonly criteria?: readonly components["schemas"]["ScorecardCriterion"][];
            /**
             * Industry Code
             * @default
             */
            readonly industry_code: string;
            /** Locations */
            readonly locations?: readonly string[];
            /** Maximum Years */
            readonly maximum_years?: number | null;
            /** Minimum Years */
            readonly minimum_years?: number | null;
            /** Seniority */
            readonly seniority?: readonly string[];
            /** Suggested Adjacent Industries */
            readonly suggested_adjacent_industries?: readonly string[];
            /** Target Titles */
            readonly target_titles?: readonly string[];
            /** Uncertainties */
            readonly uncertainties?: readonly string[];
        };
        /** EnrichmentRequestResponse */
        readonly EnrichmentRequestResponse: {
            /**
             * Id
             * Format: uuid
             */
            readonly id: string;
            /**
             * Run Id
             * Format: uuid
             */
            readonly run_id: string;
            /** Status */
            readonly status: string;
        };
        /**
         * ExtractionStatus
         * @enum {string}
         */
        readonly ExtractionStatus: "ready" | "manual_required";
        /** HTTPValidationError */
        readonly HTTPValidationError: {
            /** Detail */
            readonly detail?: readonly components["schemas"]["ValidationError"][];
        };
        /** InvitationCreate */
        readonly InvitationCreate: {
            /** Email */
            readonly email: string;
            readonly role: components["schemas"]["Role"];
        };
        /** InvitationResponse */
        readonly InvitationResponse: {
            /**
             * Expires At
             * Format: date-time
             */
            readonly expires_at: string;
            /**
             * Invitation Id
             * Format: uuid
             */
            readonly invitation_id: string;
            /** Token */
            readonly token: string;
        };
        /** JobCandidatePage */
        readonly JobCandidatePage: {
            /** Items */
            readonly items: readonly components["schemas"]["JobCandidateView"][];
            /** Next Cursor */
            readonly next_cursor: string | null;
        };
        /** JobCandidateView */
        readonly JobCandidateView: {
            /**
             * Candidate Id
             * Format: uuid
             */
            readonly candidate_id: string;
            /** Classification */
            readonly classification: string;
            /** Contacts */
            readonly contacts?: readonly components["schemas"]["MaskedContact"][] | null;
            /**
             * Created At
             * Format: date-time
             */
            readonly created_at: string;
            /** Current Company */
            readonly current_company: string | null;
            /** Current Title */
            readonly current_title: string | null;
            /** Full Name */
            readonly full_name: string;
            /** Has Contact */
            readonly has_contact: boolean;
            /**
             * Id
             * Format: uuid
             */
            readonly id: string;
            /**
             * Job Id
             * Format: uuid
             */
            readonly job_id: string;
            /** Location */
            readonly location: string | null;
            /** Owner User Id */
            readonly owner_user_id: string | null;
            /** Rejection Note */
            readonly rejection_note: string | null;
            /** Rejection Reason Code */
            readonly rejection_reason_code: string | null;
            /** Score */
            readonly score: number;
            /** Score Json */
            readonly score_json?: {
                readonly [key: string]: unknown;
            } | null;
            /**
             * Scorecard Version Id
             * Format: uuid
             */
            readonly scorecard_version_id: string;
            /** Scoring Version */
            readonly scoring_version: string;
            readonly stage: components["schemas"]["CandidateStage"];
            /** Tags */
            readonly tags: readonly string[];
            /**
             * Updated At
             * Format: date-time
             */
            readonly updated_at: string;
        };
        /** JobCreate */
        readonly JobCreate: {
            /**
             * Client Id
             * Format: uuid
             */
            readonly client_id: string;
            /** Employment Model */
            readonly employment_model?: string | null;
            /** Job Description */
            readonly job_description: string;
            /** Location */
            readonly location?: string | null;
            /** Title */
            readonly title: string;
        };
        /** JobPage */
        readonly JobPage: {
            /** Items */
            readonly items: readonly components["schemas"]["JobSummary"][];
            /** Next Offset */
            readonly next_offset: number | null;
        };
        /** JobResponse */
        readonly JobResponse: {
            /**
             * Client Id
             * Format: uuid
             */
            readonly client_id: string;
            /**
             * Created At
             * Format: date-time
             */
            readonly created_at: string;
            /** Current Scorecard Id */
            readonly current_scorecard_id: string | null;
            /** Draft Revision */
            readonly draft_revision: number;
            /** Employment Model */
            readonly employment_model: string | null;
            readonly extraction_status: components["schemas"]["ExtractionStatus"];
            /** Extraction Warning */
            readonly extraction_warning: string | null;
            /**
             * Id
             * Format: uuid
             */
            readonly id: string;
            /** Job Description */
            readonly job_description: string;
            /** Location */
            readonly location: string | null;
            /**
             * Owner User Id
             * Format: uuid
             */
            readonly owner_user_id: string;
            /** Status */
            readonly status: string;
            /**
             * Tenant Id
             * Format: uuid
             */
            readonly tenant_id: string;
            /** Title */
            readonly title: string;
            /**
             * Updated At
             * Format: date-time
             */
            readonly updated_at: string;
        };
        /** JobSummary */
        readonly JobSummary: {
            /**
             * Client Id
             * Format: uuid
             */
            readonly client_id: string;
            /**
             * Id
             * Format: uuid
             */
            readonly id: string;
            /** Location */
            readonly location: string | null;
            /** Status */
            readonly status: string;
            /** Title */
            readonly title: string;
        };
        /** MaskedContact */
        readonly MaskedContact: {
            /** Classification */
            readonly classification: string;
            /**
             * Expires At
             * Format: date-time
             */
            readonly expires_at: string;
            /**
             * Id
             * Format: uuid
             */
            readonly id: string;
            /** Kind */
            readonly kind: string;
            /** Masked Value */
            readonly masked_value: string;
            /** Verification State */
            readonly verification_state: string;
        };
        /** MemberResponse */
        readonly MemberResponse: {
            /** Active */
            readonly active: boolean;
            /** Allowed Client Ids */
            readonly allowed_client_ids: readonly string[] | null;
            /** Display Name */
            readonly display_name: string;
            /**
             * Email
             * Format: email
             */
            readonly email: string;
            /**
             * Membership Id
             * Format: uuid
             */
            readonly membership_id: string;
            readonly role: components["schemas"]["Role"];
            /**
             * User Id
             * Format: uuid
             */
            readonly user_id: string;
        };
        /** MembershipResponse */
        readonly MembershipResponse: {
            /** Active */
            readonly active: boolean;
            /**
             * Membership Id
             * Format: uuid
             */
            readonly membership_id: string;
            readonly role: components["schemas"]["Role"];
            /**
             * Tenant Id
             * Format: uuid
             */
            readonly tenant_id: string;
            /**
             * User Id
             * Format: uuid
             */
            readonly user_id: string;
        };
        /** MeResponse */
        readonly MeResponse: {
            /** Allowed Client Ids */
            readonly allowed_client_ids?: readonly string[] | null;
            /** Display Name */
            readonly display_name: string;
            /**
             * Email
             * Format: email
             */
            readonly email: string;
            readonly role: components["schemas"]["Role"];
            /**
             * Tenant Id
             * Format: uuid
             */
            readonly tenant_id: string;
            /**
             * User Id
             * Format: uuid
             */
            readonly user_id: string;
        };
        /** NoteCreate */
        readonly NoteCreate: {
            /** Body */
            readonly body: string;
        };
        /** NoteResponse */
        readonly NoteResponse: {
            /**
             * Actor User Id
             * Format: uuid
             */
            readonly actor_user_id: string;
            /** Body */
            readonly body: string;
            /**
             * Created At
             * Format: date-time
             */
            readonly created_at: string;
            /**
             * Id
             * Format: uuid
             */
            readonly id: string;
            /**
             * Job Candidate Id
             * Format: uuid
             */
            readonly job_candidate_id: string;
            /**
             * Updated At
             * Format: date-time
             */
            readonly updated_at: string;
        };
        /** NotificationResponse */
        readonly NotificationResponse: {
            /** Acknowledged At */
            readonly acknowledged_at: string | null;
            /** Code */
            readonly code: string;
            /**
             * Created At
             * Format: date-time
             */
            readonly created_at: string;
            /**
             * Id
             * Format: uuid
             */
            readonly id: string;
            /** Message */
            readonly message: string;
            /** Run Id */
            readonly run_id: string | null;
            /** Title */
            readonly title: string;
        };
        /** OwnerUpdate */
        readonly OwnerUpdate: {
            /** Owner User Id */
            readonly owner_user_id: string | null;
        };
        /** PrivacyRequestCreate */
        readonly PrivacyRequestCreate: {
            /**
             * Candidate Id
             * Format: uuid
             */
            readonly candidate_id: string;
            readonly request_type: components["schemas"]["PrivacyRequestType"];
        };
        /** PrivacyRequestReject */
        readonly PrivacyRequestReject: {
            /** Reason Code */
            readonly reason_code: string;
        };
        /** PrivacyRequestResponse */
        readonly PrivacyRequestResponse: {
            /** Approved At */
            readonly approved_at: string | null;
            /**
             * Candidate Id
             * Format: uuid
             */
            readonly candidate_id: string;
            /** Completed At */
            readonly completed_at: string | null;
            /**
             * Created At
             * Format: date-time
             */
            readonly created_at: string;
            /**
             * Id
             * Format: uuid
             */
            readonly id: string;
            /** Identity Verified At */
            readonly identity_verified_at: string | null;
            /** Rejected At */
            readonly rejected_at: string | null;
            /** Rejection Reason Code */
            readonly rejection_reason_code: string | null;
            readonly request_type: components["schemas"]["PrivacyRequestType"];
            readonly state: components["schemas"]["PrivacyRequestState"];
            /**
             * Tenant Id
             * Format: uuid
             */
            readonly tenant_id: string;
            /**
             * Updated At
             * Format: date-time
             */
            readonly updated_at: string;
        };
        /**
         * PrivacyRequestState
         * @enum {string}
         */
        readonly PrivacyRequestState: "Received" | "Identity Verification Required" | "Approved" | "Executing" | "Completed" | "Rejected";
        /**
         * PrivacyRequestType
         * @enum {string}
         */
        readonly PrivacyRequestType: "Access" | "Correction" | "Deletion" | "Opt Out";
        /**
         * Role
         * @enum {string}
         */
        readonly Role: "owner" | "admin" | "recruiter";
        /** RoleUpdate */
        readonly RoleUpdate: {
            readonly role: components["schemas"]["Role"];
        };
        /** RunActivityResponse */
        readonly RunActivityResponse: {
            /** Action */
            readonly action: string;
            /** Actor User Id */
            readonly actor_user_id: string | null;
            /**
             * Created At
             * Format: date-time
             */
            readonly created_at: string;
            /**
             * Entity Id
             * Format: uuid
             */
            readonly entity_id: string;
            /** Entity Type */
            readonly entity_type: string;
            /**
             * Id
             * Format: uuid
             */
            readonly id: string;
            /** Payload */
            readonly payload: {
                readonly [key: string]: unknown;
            };
        };
        /** RunResponse */
        readonly RunResponse: {
            /** Budget Use */
            readonly budget_use: {
                readonly [key: string]: number;
            };
            /** Cancellation Requested */
            readonly cancellation_requested: boolean;
            /** Candidate Count */
            readonly candidate_count: number;
            /** Completed At */
            readonly completed_at: string | null;
            /**
             * Created At
             * Format: date-time
             */
            readonly created_at: string;
            /** Current Stage */
            readonly current_stage: string;
            /** Error Code */
            readonly error_code: string | null;
            /** Error Message */
            readonly error_message: string | null;
            /**
             * Id
             * Format: uuid
             */
            readonly id: string;
            /**
             * Job Id
             * Format: uuid
             */
            readonly job_id: string;
            /** Matched Count */
            readonly matched_count: number;
            /**
             * Scorecard Version Id
             * Format: uuid
             */
            readonly scorecard_version_id: string;
            /** Started At */
            readonly started_at: string | null;
            readonly state: components["schemas"]["RunState"];
            /**
             * Tenant Id
             * Format: uuid
             */
            readonly tenant_id: string;
            /**
             * Updated At
             * Format: date-time
             */
            readonly updated_at: string;
        };
        /**
         * RunState
         * @enum {string}
         */
        readonly RunState: "queued" | "sourcing" | "matching" | "enriching" | "partially_ready" | "ready" | "cancelled" | "failed";
        /** ScorecardConfirmation */
        readonly ScorecardConfirmation: {
            /** Expected Revision */
            readonly expected_revision: number;
        };
        /** ScorecardCriterion */
        readonly ScorecardCriterion: {
            /**
             * Evidence Required
             * @default false
             */
            readonly evidence_required: boolean;
            /**
             * Inferred
             * @default false
             */
            readonly inferred: boolean;
            /** Key */
            readonly key: string;
            readonly kind: components["schemas"]["CriterionKind"];
            /** Label */
            readonly label: string;
            /**
             * Lawful Requirement Confirmed
             * @default false
             */
            readonly lawful_requirement_confirmed: boolean;
            /**
             * Recruiter Entered
             * @default false
             */
            readonly recruiter_entered: boolean;
            /** Source Text */
            readonly source_text?: string | null;
        };
        /** ScorecardDraft */
        readonly ScorecardDraft: {
            /** Confirmed Inferred Items */
            readonly confirmed_inferred_items?: readonly string[];
            /** Criteria */
            readonly criteria: readonly components["schemas"]["ScorecardCriterion"][];
            /** Industry Code */
            readonly industry_code: string;
            /** Locations */
            readonly locations: readonly string[];
            /** Maximum Years */
            readonly maximum_years?: number | null;
            /** Minimum Years */
            readonly minimum_years?: number | null;
            /** Seniority */
            readonly seniority: readonly string[];
            /** Suggested Adjacent Industries */
            readonly suggested_adjacent_industries: readonly string[];
            /** Target Titles */
            readonly target_titles: readonly string[];
            /** Uncertainties */
            readonly uncertainties: readonly string[];
        };
        /** ScorecardDraftResponse */
        readonly ScorecardDraftResponse: {
            /** Draft */
            readonly draft: components["schemas"]["ScorecardDraft"] | components["schemas"]["EditableScorecardDraft"];
            /** Draft Revision */
            readonly draft_revision: number;
            readonly extraction_status: components["schemas"]["ExtractionStatus"];
            /** Extraction Warning */
            readonly extraction_warning: string | null;
            /**
             * Job Id
             * Format: uuid
             */
            readonly job_id: string;
            /** Original Job Description */
            readonly original_job_description: string;
        };
        /** ScorecardDraftUpdate */
        readonly ScorecardDraftUpdate: {
            readonly draft: components["schemas"]["ScorecardDraft"];
            /** Expected Revision */
            readonly expected_revision: number;
        };
        /** ScorecardGenerationRequest */
        readonly ScorecardGenerationRequest: {
            /** Expected Revision */
            readonly expected_revision: number;
        };
        /** ScorecardRevisionRequest */
        readonly ScorecardRevisionRequest: {
            readonly draft: components["schemas"]["ScorecardDraft"];
            /** Expected Revision */
            readonly expected_revision: number;
        };
        /** StageUpdate */
        readonly StageUpdate: {
            /** Note */
            readonly note?: string | null;
            /** Reason Code */
            readonly reason_code?: string | null;
            readonly stage: components["schemas"]["CandidateStage"];
        };
        /** StartRunRequest */
        readonly StartRunRequest: Record<string, never>;
        /** TagsResponse */
        readonly TagsResponse: {
            /**
             * Job Candidate Id
             * Format: uuid
             */
            readonly job_candidate_id: string;
            /** Tags */
            readonly tags: readonly string[];
        };
        /** TagsUpdate */
        readonly TagsUpdate: {
            /** Tags */
            readonly tags: readonly string[];
        };
        /** ValidationError */
        readonly ValidationError: {
            /** Context */
            readonly ctx?: Record<string, never>;
            /** Input */
            readonly input?: unknown;
            /** Location */
            readonly loc: readonly (string | number)[];
            /** Message */
            readonly msg: string;
            /** Error Type */
            readonly type: string;
        };
    };
    responses: never;
    parameters: never;
    requestBodies: never;
    headers: never;
    pathItems: never;
}
export type $defs = Record<string, never>;
export interface operations {
    readonly directory_api_v1_candidates_get: {
        readonly parameters: {
            readonly query?: {
                readonly cursor?: string | null;
                readonly industry?: string | null;
                readonly limit?: number;
                readonly location?: string | null;
                readonly q?: string | null;
            };
            readonly header?: never;
            readonly path?: never;
            readonly cookie?: never;
        };
        readonly requestBody?: never;
        readonly responses: {
            /** @description Successful Response */
            readonly 200: {
                headers: {
                    readonly [name: string]: unknown;
                };
                content: {
                    readonly "application/json": components["schemas"]["CandidateDirectoryPage"];
                };
            };
            /** @description Validation Error */
            readonly 422: {
                headers: {
                    readonly [name: string]: unknown;
                };
                content: {
                    readonly "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    readonly candidate_jobs_api_v1_candidates__candidate_id__jobs_get: {
        readonly parameters: {
            readonly query?: never;
            readonly header?: never;
            readonly path: {
                readonly candidate_id: string;
            };
            readonly cookie?: never;
        };
        readonly requestBody?: never;
        readonly responses: {
            /** @description Successful Response */
            readonly 200: {
                headers: {
                    readonly [name: string]: unknown;
                };
                content: {
                    readonly "application/json": readonly components["schemas"]["CandidateJobView"][];
                };
            };
            /** @description Validation Error */
            readonly 422: {
                headers: {
                    readonly [name: string]: unknown;
                };
                content: {
                    readonly "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    readonly list_clients_api_v1_clients_get: {
        readonly parameters: {
            readonly query?: never;
            readonly header?: never;
            readonly path?: never;
            readonly cookie?: never;
        };
        readonly requestBody?: never;
        readonly responses: {
            /** @description Successful Response */
            readonly 200: {
                headers: {
                    readonly [name: string]: unknown;
                };
                content: {
                    readonly "application/json": readonly components["schemas"]["ClientResponse"][];
                };
            };
        };
    };
    readonly create_client_api_v1_clients_post: {
        readonly parameters: {
            readonly query?: never;
            readonly header?: never;
            readonly path?: never;
            readonly cookie?: never;
        };
        readonly requestBody: {
            readonly content: {
                readonly "application/json": components["schemas"]["ClientCreate"];
            };
        };
        readonly responses: {
            /** @description Successful Response */
            readonly 201: {
                headers: {
                    readonly [name: string]: unknown;
                };
                content: {
                    readonly "application/json": components["schemas"]["ClientResponse"];
                };
            };
            /** @description Validation Error */
            readonly 422: {
                headers: {
                    readonly [name: string]: unknown;
                };
                content: {
                    readonly "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    readonly get_client_api_v1_clients__client_id__get: {
        readonly parameters: {
            readonly query?: never;
            readonly header?: never;
            readonly path: {
                readonly client_id: string;
            };
            readonly cookie?: never;
        };
        readonly requestBody?: never;
        readonly responses: {
            /** @description Successful Response */
            readonly 200: {
                headers: {
                    readonly [name: string]: unknown;
                };
                content: {
                    readonly "application/json": components["schemas"]["ClientResponse"];
                };
            };
            /** @description Validation Error */
            readonly 422: {
                headers: {
                    readonly [name: string]: unknown;
                };
                content: {
                    readonly "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    readonly approve_client_adjacency_api_v1_clients__client_id__adjacent_industries_put: {
        readonly parameters: {
            readonly query?: never;
            readonly header?: never;
            readonly path: {
                readonly client_id: string;
            };
            readonly cookie?: never;
        };
        readonly requestBody: {
            readonly content: {
                readonly "application/json": components["schemas"]["ClientAdjacencyUpdate"];
            };
        };
        readonly responses: {
            /** @description Successful Response */
            readonly 200: {
                headers: {
                    readonly [name: string]: unknown;
                };
                content: {
                    readonly "application/json": components["schemas"]["ClientResponse"];
                };
            };
            /** @description Validation Error */
            readonly 422: {
                headers: {
                    readonly [name: string]: unknown;
                };
                content: {
                    readonly "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    readonly grant_client_access_api_v1_clients__client_id__grants_post: {
        readonly parameters: {
            readonly query?: never;
            readonly header?: never;
            readonly path: {
                readonly client_id: string;
            };
            readonly cookie?: never;
        };
        readonly requestBody: {
            readonly content: {
                readonly "application/json": components["schemas"]["ClientGrantCreate"];
            };
        };
        readonly responses: {
            /** @description Successful Response */
            readonly 200: {
                headers: {
                    readonly [name: string]: unknown;
                };
                content: {
                    readonly "application/json": components["schemas"]["ClientGrantResponse"];
                };
            };
            /** @description Validation Error */
            readonly 422: {
                headers: {
                    readonly [name: string]: unknown;
                };
                content: {
                    readonly "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    readonly update_client_industries_api_v1_clients__client_id__industries_put: {
        readonly parameters: {
            readonly query?: never;
            readonly header?: never;
            readonly path: {
                readonly client_id: string;
            };
            readonly cookie?: never;
        };
        readonly requestBody: {
            readonly content: {
                readonly "application/json": components["schemas"]["ClientIndustriesUpdate"];
            };
        };
        readonly responses: {
            /** @description Successful Response */
            readonly 200: {
                headers: {
                    readonly [name: string]: unknown;
                };
                content: {
                    readonly "application/json": components["schemas"]["ClientResponse"];
                };
            };
            /** @description Validation Error */
            readonly 422: {
                headers: {
                    readonly [name: string]: unknown;
                };
                content: {
                    readonly "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    readonly reveal_contact_api_v1_contact_points__contact_point_id__reveal_post: {
        readonly parameters: {
            readonly query?: never;
            readonly header?: never;
            readonly path: {
                readonly contact_point_id: string;
            };
            readonly cookie?: never;
        };
        readonly requestBody?: never;
        readonly responses: {
            /** @description Successful Response */
            readonly 200: {
                headers: {
                    readonly [name: string]: unknown;
                };
                content: {
                    readonly "application/json": components["schemas"]["ContactRevealResponse"];
                };
            };
            /** @description Validation Error */
            readonly 422: {
                headers: {
                    readonly [name: string]: unknown;
                };
                content: {
                    readonly "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    readonly get_job_candidate_api_v1_job_candidates__job_candidate_id__get: {
        readonly parameters: {
            readonly query?: never;
            readonly header?: never;
            readonly path: {
                readonly job_candidate_id: string;
            };
            readonly cookie?: never;
        };
        readonly requestBody?: never;
        readonly responses: {
            /** @description Successful Response */
            readonly 200: {
                headers: {
                    readonly [name: string]: unknown;
                };
                content: {
                    readonly "application/json": components["schemas"]["JobCandidateView"];
                };
            };
            /** @description Validation Error */
            readonly 422: {
                headers: {
                    readonly [name: string]: unknown;
                };
                content: {
                    readonly "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    readonly list_activity_api_v1_job_candidates__job_candidate_id__activity_get: {
        readonly parameters: {
            readonly query?: {
                readonly cursor?: string | null;
                readonly limit?: number;
            };
            readonly header?: never;
            readonly path: {
                readonly job_candidate_id: string;
            };
            readonly cookie?: never;
        };
        readonly requestBody?: never;
        readonly responses: {
            /** @description Successful Response */
            readonly 200: {
                headers: {
                    readonly [name: string]: unknown;
                };
                content: {
                    readonly "application/json": components["schemas"]["ActivityPage"];
                };
            };
            /** @description Validation Error */
            readonly 422: {
                headers: {
                    readonly [name: string]: unknown;
                };
                content: {
                    readonly "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    readonly add_note_api_v1_job_candidates__job_candidate_id__notes_post: {
        readonly parameters: {
            readonly query?: never;
            readonly header?: never;
            readonly path: {
                readonly job_candidate_id: string;
            };
            readonly cookie?: never;
        };
        readonly requestBody: {
            readonly content: {
                readonly "application/json": components["schemas"]["NoteCreate"];
            };
        };
        readonly responses: {
            /** @description Successful Response */
            readonly 200: {
                headers: {
                    readonly [name: string]: unknown;
                };
                content: {
                    readonly "application/json": components["schemas"]["NoteResponse"];
                };
            };
            /** @description Validation Error */
            readonly 422: {
                headers: {
                    readonly [name: string]: unknown;
                };
                content: {
                    readonly "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    readonly update_owner_api_v1_job_candidates__job_candidate_id__owner_patch: {
        readonly parameters: {
            readonly query?: never;
            readonly header?: never;
            readonly path: {
                readonly job_candidate_id: string;
            };
            readonly cookie?: never;
        };
        readonly requestBody: {
            readonly content: {
                readonly "application/json": components["schemas"]["OwnerUpdate"];
            };
        };
        readonly responses: {
            /** @description Successful Response */
            readonly 200: {
                headers: {
                    readonly [name: string]: unknown;
                };
                content: {
                    readonly "application/json": components["schemas"]["JobCandidateView"];
                };
            };
            /** @description Validation Error */
            readonly 422: {
                headers: {
                    readonly [name: string]: unknown;
                };
                content: {
                    readonly "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    readonly update_stage_api_v1_job_candidates__job_candidate_id__stage_patch: {
        readonly parameters: {
            readonly query?: never;
            readonly header?: never;
            readonly path: {
                readonly job_candidate_id: string;
            };
            readonly cookie?: never;
        };
        readonly requestBody: {
            readonly content: {
                readonly "application/json": components["schemas"]["StageUpdate"];
            };
        };
        readonly responses: {
            /** @description Successful Response */
            readonly 200: {
                headers: {
                    readonly [name: string]: unknown;
                };
                content: {
                    readonly "application/json": components["schemas"]["JobCandidateView"];
                };
            };
            /** @description Validation Error */
            readonly 422: {
                headers: {
                    readonly [name: string]: unknown;
                };
                content: {
                    readonly "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    readonly update_tags_api_v1_job_candidates__job_candidate_id__tags_put: {
        readonly parameters: {
            readonly query?: never;
            readonly header?: never;
            readonly path: {
                readonly job_candidate_id: string;
            };
            readonly cookie?: never;
        };
        readonly requestBody: {
            readonly content: {
                readonly "application/json": components["schemas"]["TagsUpdate"];
            };
        };
        readonly responses: {
            /** @description Successful Response */
            readonly 200: {
                headers: {
                    readonly [name: string]: unknown;
                };
                content: {
                    readonly "application/json": components["schemas"]["TagsResponse"];
                };
            };
            /** @description Validation Error */
            readonly 422: {
                headers: {
                    readonly [name: string]: unknown;
                };
                content: {
                    readonly "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    readonly request_candidate_enrichment_api_v1_job_candidates__run_candidate_id__enrich_post: {
        readonly parameters: {
            readonly query?: never;
            readonly header?: never;
            readonly path: {
                readonly run_candidate_id: string;
            };
            readonly cookie?: never;
        };
        readonly requestBody?: never;
        readonly responses: {
            /** @description Successful Response */
            readonly 202: {
                headers: {
                    readonly [name: string]: unknown;
                };
                content: {
                    readonly "application/json": components["schemas"]["EnrichmentRequestResponse"];
                };
            };
            /** @description Validation Error */
            readonly 422: {
                headers: {
                    readonly [name: string]: unknown;
                };
                content: {
                    readonly "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    readonly list_jobs_api_v1_jobs_get: {
        readonly parameters: {
            readonly query?: {
                readonly limit?: number;
                readonly offset?: number;
            };
            readonly header?: never;
            readonly path?: never;
            readonly cookie?: never;
        };
        readonly requestBody?: never;
        readonly responses: {
            /** @description Successful Response */
            readonly 200: {
                headers: {
                    readonly [name: string]: unknown;
                };
                content: {
                    readonly "application/json": components["schemas"]["JobPage"];
                };
            };
            /** @description Validation Error */
            readonly 422: {
                headers: {
                    readonly [name: string]: unknown;
                };
                content: {
                    readonly "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    readonly create_job_api_v1_jobs_post: {
        readonly parameters: {
            readonly query?: never;
            readonly header?: never;
            readonly path?: never;
            readonly cookie?: never;
        };
        readonly requestBody: {
            readonly content: {
                readonly "application/json": components["schemas"]["JobCreate"];
            };
        };
        readonly responses: {
            /** @description Successful Response */
            readonly 201: {
                headers: {
                    readonly [name: string]: unknown;
                };
                content: {
                    readonly "application/json": components["schemas"]["JobResponse"];
                };
            };
            /** @description Validation Error */
            readonly 422: {
                headers: {
                    readonly [name: string]: unknown;
                };
                content: {
                    readonly "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    readonly get_job_api_v1_jobs__job_id__get: {
        readonly parameters: {
            readonly query?: never;
            readonly header?: never;
            readonly path: {
                readonly job_id: string;
            };
            readonly cookie?: never;
        };
        readonly requestBody?: never;
        readonly responses: {
            /** @description Successful Response */
            readonly 200: {
                headers: {
                    readonly [name: string]: unknown;
                };
                content: {
                    readonly "application/json": components["schemas"]["JobResponse"];
                };
            };
            /** @description Validation Error */
            readonly 422: {
                headers: {
                    readonly [name: string]: unknown;
                };
                content: {
                    readonly "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    readonly acceptance_api_v1_jobs__job_id__acceptance_get: {
        readonly parameters: {
            readonly query?: never;
            readonly header?: never;
            readonly path: {
                readonly job_id: string;
            };
            readonly cookie?: never;
        };
        readonly requestBody?: never;
        readonly responses: {
            /** @description Successful Response */
            readonly 200: {
                headers: {
                    readonly [name: string]: unknown;
                };
                content: {
                    readonly "application/json": components["schemas"]["AcceptanceResponse"];
                };
            };
            /** @description Validation Error */
            readonly 422: {
                headers: {
                    readonly [name: string]: unknown;
                };
                content: {
                    readonly "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    readonly list_job_candidates_api_v1_jobs__job_id__candidates_get: {
        readonly parameters: {
            readonly query?: {
                readonly classification?: string;
                readonly cursor?: string | null;
                readonly has_contact?: boolean | null;
                readonly industry?: string | null;
                readonly limit?: number;
                readonly location?: string | null;
                readonly owner?: string | null;
                readonly q?: string | null;
                readonly score_max?: number | null;
                readonly score_min?: number | null;
                readonly sort?: string;
                readonly stage?: components["schemas"]["CandidateStage"] | null;
                readonly tags?: string | null;
            };
            readonly header?: never;
            readonly path: {
                readonly job_id: string;
            };
            readonly cookie?: never;
        };
        readonly requestBody?: never;
        readonly responses: {
            /** @description Successful Response */
            readonly 200: {
                headers: {
                    readonly [name: string]: unknown;
                };
                content: {
                    readonly "application/json": components["schemas"]["JobCandidatePage"];
                };
            };
            /** @description Validation Error */
            readonly 422: {
                headers: {
                    readonly [name: string]: unknown;
                };
                content: {
                    readonly "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    readonly export_shortlist_api_v1_jobs__job_id__export_csv_get: {
        readonly parameters: {
            readonly query?: never;
            readonly header?: never;
            readonly path: {
                readonly job_id: string;
            };
            readonly cookie?: never;
        };
        readonly requestBody?: never;
        readonly responses: {
            /** @description Successful Response */
            readonly 200: {
                headers: {
                    readonly [name: string]: unknown;
                };
                content: {
                    readonly "application/json": unknown;
                };
            };
            /** @description Validation Error */
            readonly 422: {
                headers: {
                    readonly [name: string]: unknown;
                };
                content: {
                    readonly "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    readonly rescore_job_api_v1_jobs__job_id__rescore_post: {
        readonly parameters: {
            readonly query?: never;
            readonly header?: never;
            readonly path: {
                readonly job_id: string;
            };
            readonly cookie?: never;
        };
        readonly requestBody: {
            readonly content: {
                readonly "application/json": components["schemas"]["ScorecardRevisionRequest"];
            };
        };
        readonly responses: {
            /** @description Successful Response */
            readonly 200: {
                headers: {
                    readonly [name: string]: unknown;
                };
                content: {
                    readonly "application/json": components["schemas"]["ConfirmedScorecard"];
                };
            };
            /** @description Validation Error */
            readonly 422: {
                headers: {
                    readonly [name: string]: unknown;
                };
                content: {
                    readonly "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    readonly start_run_api_v1_jobs__job_id__runs_post: {
        readonly parameters: {
            readonly query?: never;
            readonly header?: never;
            readonly path: {
                readonly job_id: string;
            };
            readonly cookie?: never;
        };
        readonly requestBody: {
            readonly content: {
                readonly "application/json": components["schemas"]["StartRunRequest"];
            };
        };
        readonly responses: {
            /** @description Successful Response */
            readonly 201: {
                headers: {
                    readonly [name: string]: unknown;
                };
                content: {
                    readonly "application/json": components["schemas"]["RunResponse"];
                };
            };
            /** @description Validation Error */
            readonly 422: {
                headers: {
                    readonly [name: string]: unknown;
                };
                content: {
                    readonly "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    readonly confirm_scorecard_api_v1_jobs__job_id__scorecard_confirm_post: {
        readonly parameters: {
            readonly query?: never;
            readonly header?: never;
            readonly path: {
                readonly job_id: string;
            };
            readonly cookie?: never;
        };
        readonly requestBody: {
            readonly content: {
                readonly "application/json": components["schemas"]["ScorecardConfirmation"];
            };
        };
        readonly responses: {
            /** @description Successful Response */
            readonly 200: {
                headers: {
                    readonly [name: string]: unknown;
                };
                content: {
                    readonly "application/json": components["schemas"]["ConfirmedScorecard"];
                };
            };
            /** @description Validation Error */
            readonly 422: {
                headers: {
                    readonly [name: string]: unknown;
                };
                content: {
                    readonly "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    readonly get_scorecard_draft_api_v1_jobs__job_id__scorecard_draft_get: {
        readonly parameters: {
            readonly query?: never;
            readonly header?: never;
            readonly path: {
                readonly job_id: string;
            };
            readonly cookie?: never;
        };
        readonly requestBody?: never;
        readonly responses: {
            /** @description Successful Response */
            readonly 200: {
                headers: {
                    readonly [name: string]: unknown;
                };
                content: {
                    readonly "application/json": components["schemas"]["ScorecardDraftResponse"];
                };
            };
            /** @description Validation Error */
            readonly 422: {
                headers: {
                    readonly [name: string]: unknown;
                };
                content: {
                    readonly "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    readonly update_scorecard_draft_api_v1_jobs__job_id__scorecard_draft_put: {
        readonly parameters: {
            readonly query?: never;
            readonly header?: never;
            readonly path: {
                readonly job_id: string;
            };
            readonly cookie?: never;
        };
        readonly requestBody: {
            readonly content: {
                readonly "application/json": components["schemas"]["ScorecardDraftUpdate"];
            };
        };
        readonly responses: {
            /** @description Successful Response */
            readonly 200: {
                headers: {
                    readonly [name: string]: unknown;
                };
                content: {
                    readonly "application/json": components["schemas"]["ScorecardDraftResponse"];
                };
            };
            /** @description Validation Error */
            readonly 422: {
                headers: {
                    readonly [name: string]: unknown;
                };
                content: {
                    readonly "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    readonly generate_scorecard_draft_api_v1_jobs__job_id__scorecard_generate_post: {
        readonly parameters: {
            readonly query?: never;
            readonly header?: never;
            readonly path: {
                readonly job_id: string;
            };
            readonly cookie?: never;
        };
        readonly requestBody: {
            readonly content: {
                readonly "application/json": components["schemas"]["ScorecardGenerationRequest"];
            };
        };
        readonly responses: {
            /** @description Successful Response */
            readonly 200: {
                headers: {
                    readonly [name: string]: unknown;
                };
                content: {
                    readonly "application/json": components["schemas"]["ScorecardDraftResponse"];
                };
            };
            /** @description Validation Error */
            readonly 422: {
                headers: {
                    readonly [name: string]: unknown;
                };
                content: {
                    readonly "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    readonly list_scorecard_versions_api_v1_jobs__job_id__scorecards_get: {
        readonly parameters: {
            readonly query?: never;
            readonly header?: never;
            readonly path: {
                readonly job_id: string;
            };
            readonly cookie?: never;
        };
        readonly requestBody?: never;
        readonly responses: {
            /** @description Successful Response */
            readonly 200: {
                headers: {
                    readonly [name: string]: unknown;
                };
                content: {
                    readonly "application/json": readonly components["schemas"]["ConfirmedScorecard"][];
                };
            };
            /** @description Validation Error */
            readonly 422: {
                headers: {
                    readonly [name: string]: unknown;
                };
                content: {
                    readonly "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    readonly me_api_v1_me_get: {
        readonly parameters: {
            readonly query?: never;
            readonly header?: never;
            readonly path?: never;
            readonly cookie?: never;
        };
        readonly requestBody?: never;
        readonly responses: {
            /** @description Successful Response */
            readonly 200: {
                headers: {
                    readonly [name: string]: unknown;
                };
                content: {
                    readonly "application/json": components["schemas"]["MeResponse"];
                };
            };
        };
    };
    readonly list_members_api_v1_members_get: {
        readonly parameters: {
            readonly query?: never;
            readonly header?: never;
            readonly path?: never;
            readonly cookie?: never;
        };
        readonly requestBody?: never;
        readonly responses: {
            /** @description Successful Response */
            readonly 200: {
                headers: {
                    readonly [name: string]: unknown;
                };
                content: {
                    readonly "application/json": readonly components["schemas"]["MemberResponse"][];
                };
            };
        };
    };
    readonly deactivate_member_api_v1_members__membership_id__delete: {
        readonly parameters: {
            readonly query?: never;
            readonly header?: never;
            readonly path: {
                readonly membership_id: string;
            };
            readonly cookie?: never;
        };
        readonly requestBody?: never;
        readonly responses: {
            /** @description Successful Response */
            readonly 204: {
                headers: {
                    readonly [name: string]: unknown;
                };
                content?: never;
            };
            /** @description Validation Error */
            readonly 422: {
                headers: {
                    readonly [name: string]: unknown;
                };
                content: {
                    readonly "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    readonly change_member_role_api_v1_members__membership_id__role_patch: {
        readonly parameters: {
            readonly query?: never;
            readonly header?: never;
            readonly path: {
                readonly membership_id: string;
            };
            readonly cookie?: never;
        };
        readonly requestBody: {
            readonly content: {
                readonly "application/json": components["schemas"]["RoleUpdate"];
            };
        };
        readonly responses: {
            /** @description Successful Response */
            readonly 200: {
                headers: {
                    readonly [name: string]: unknown;
                };
                content: {
                    readonly "application/json": components["schemas"]["MembershipResponse"];
                };
            };
            /** @description Validation Error */
            readonly 422: {
                headers: {
                    readonly [name: string]: unknown;
                };
                content: {
                    readonly "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    readonly create_invitation_api_v1_membership_invitations_post: {
        readonly parameters: {
            readonly query?: never;
            readonly header?: never;
            readonly path?: never;
            readonly cookie?: never;
        };
        readonly requestBody: {
            readonly content: {
                readonly "application/json": components["schemas"]["InvitationCreate"];
            };
        };
        readonly responses: {
            /** @description Successful Response */
            readonly 201: {
                headers: {
                    readonly [name: string]: unknown;
                };
                content: {
                    readonly "application/json": components["schemas"]["InvitationResponse"];
                };
            };
            /** @description Validation Error */
            readonly 422: {
                headers: {
                    readonly [name: string]: unknown;
                };
                content: {
                    readonly "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    readonly claim_invitation_api_v1_membership_invitations__token__claim_post: {
        readonly parameters: {
            readonly query?: never;
            readonly header?: never;
            readonly path: {
                readonly token: string;
            };
            readonly cookie?: never;
        };
        readonly requestBody?: never;
        readonly responses: {
            /** @description Successful Response */
            readonly 200: {
                headers: {
                    readonly [name: string]: unknown;
                };
                content: {
                    readonly "application/json": components["schemas"]["MembershipResponse"];
                };
            };
            /** @description Validation Error */
            readonly 422: {
                headers: {
                    readonly [name: string]: unknown;
                };
                content: {
                    readonly "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    readonly list_notifications_api_v1_notifications_get: {
        readonly parameters: {
            readonly query?: never;
            readonly header?: never;
            readonly path?: never;
            readonly cookie?: never;
        };
        readonly requestBody?: never;
        readonly responses: {
            /** @description Successful Response */
            readonly 200: {
                headers: {
                    readonly [name: string]: unknown;
                };
                content: {
                    readonly "application/json": readonly components["schemas"]["NotificationResponse"][];
                };
            };
        };
    };
    readonly acknowledge_notification_api_v1_notifications__notification_id__patch: {
        readonly parameters: {
            readonly query?: never;
            readonly header?: never;
            readonly path: {
                readonly notification_id: string;
            };
            readonly cookie?: never;
        };
        readonly requestBody?: never;
        readonly responses: {
            /** @description Successful Response */
            readonly 200: {
                headers: {
                    readonly [name: string]: unknown;
                };
                content: {
                    readonly "application/json": components["schemas"]["NotificationResponse"];
                };
            };
            /** @description Validation Error */
            readonly 422: {
                headers: {
                    readonly [name: string]: unknown;
                };
                content: {
                    readonly "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    readonly list_privacy_requests_api_v1_privacy_requests_get: {
        readonly parameters: {
            readonly query?: never;
            readonly header?: never;
            readonly path?: never;
            readonly cookie?: never;
        };
        readonly requestBody?: never;
        readonly responses: {
            /** @description Successful Response */
            readonly 200: {
                headers: {
                    readonly [name: string]: unknown;
                };
                content: {
                    readonly "application/json": readonly components["schemas"]["PrivacyRequestResponse"][];
                };
            };
        };
    };
    readonly submit_privacy_request_api_v1_privacy_requests_post: {
        readonly parameters: {
            readonly query?: never;
            readonly header?: never;
            readonly path?: never;
            readonly cookie?: never;
        };
        readonly requestBody: {
            readonly content: {
                readonly "application/json": components["schemas"]["PrivacyRequestCreate"];
            };
        };
        readonly responses: {
            /** @description Successful Response */
            readonly 201: {
                headers: {
                    readonly [name: string]: unknown;
                };
                content: {
                    readonly "application/json": components["schemas"]["PrivacyRequestResponse"];
                };
            };
            /** @description Validation Error */
            readonly 422: {
                headers: {
                    readonly [name: string]: unknown;
                };
                content: {
                    readonly "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    readonly privacy_request_status_api_v1_privacy_requests__request_id__get: {
        readonly parameters: {
            readonly query?: never;
            readonly header?: never;
            readonly path: {
                readonly request_id: string;
            };
            readonly cookie?: never;
        };
        readonly requestBody?: never;
        readonly responses: {
            /** @description Successful Response */
            readonly 200: {
                headers: {
                    readonly [name: string]: unknown;
                };
                content: {
                    readonly "application/json": components["schemas"]["PrivacyRequestResponse"];
                };
            };
            /** @description Validation Error */
            readonly 422: {
                headers: {
                    readonly [name: string]: unknown;
                };
                content: {
                    readonly "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    readonly approve_privacy_request_api_v1_privacy_requests__request_id__approve_post: {
        readonly parameters: {
            readonly query?: never;
            readonly header?: never;
            readonly path: {
                readonly request_id: string;
            };
            readonly cookie?: never;
        };
        readonly requestBody?: never;
        readonly responses: {
            /** @description Successful Response */
            readonly 200: {
                headers: {
                    readonly [name: string]: unknown;
                };
                content: {
                    readonly "application/json": components["schemas"]["PrivacyRequestResponse"];
                };
            };
            /** @description Validation Error */
            readonly 422: {
                headers: {
                    readonly [name: string]: unknown;
                };
                content: {
                    readonly "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    readonly execute_privacy_request_api_v1_privacy_requests__request_id__execute_post: {
        readonly parameters: {
            readonly query?: never;
            readonly header?: never;
            readonly path: {
                readonly request_id: string;
            };
            readonly cookie?: never;
        };
        readonly requestBody?: never;
        readonly responses: {
            /** @description Successful Response */
            readonly 200: {
                headers: {
                    readonly [name: string]: unknown;
                };
                content: {
                    readonly "application/json": components["schemas"]["PrivacyRequestResponse"];
                };
            };
            /** @description Validation Error */
            readonly 422: {
                headers: {
                    readonly [name: string]: unknown;
                };
                content: {
                    readonly "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    readonly reject_privacy_request_api_v1_privacy_requests__request_id__reject_post: {
        readonly parameters: {
            readonly query?: never;
            readonly header?: never;
            readonly path: {
                readonly request_id: string;
            };
            readonly cookie?: never;
        };
        readonly requestBody: {
            readonly content: {
                readonly "application/json": components["schemas"]["PrivacyRequestReject"];
            };
        };
        readonly responses: {
            /** @description Successful Response */
            readonly 200: {
                headers: {
                    readonly [name: string]: unknown;
                };
                content: {
                    readonly "application/json": components["schemas"]["PrivacyRequestResponse"];
                };
            };
            /** @description Validation Error */
            readonly 422: {
                headers: {
                    readonly [name: string]: unknown;
                };
                content: {
                    readonly "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    readonly verify_privacy_request_api_v1_privacy_requests__request_id__verify_post: {
        readonly parameters: {
            readonly query?: never;
            readonly header?: never;
            readonly path: {
                readonly request_id: string;
            };
            readonly cookie?: never;
        };
        readonly requestBody?: never;
        readonly responses: {
            /** @description Successful Response */
            readonly 200: {
                headers: {
                    readonly [name: string]: unknown;
                };
                content: {
                    readonly "application/json": components["schemas"]["PrivacyRequestResponse"];
                };
            };
            /** @description Validation Error */
            readonly 422: {
                headers: {
                    readonly [name: string]: unknown;
                };
                content: {
                    readonly "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    readonly get_run_api_v1_runs__run_id__get: {
        readonly parameters: {
            readonly query?: never;
            readonly header?: never;
            readonly path: {
                readonly run_id: string;
            };
            readonly cookie?: never;
        };
        readonly requestBody?: never;
        readonly responses: {
            /** @description Successful Response */
            readonly 200: {
                headers: {
                    readonly [name: string]: unknown;
                };
                content: {
                    readonly "application/json": components["schemas"]["RunResponse"];
                };
            };
            /** @description Validation Error */
            readonly 422: {
                headers: {
                    readonly [name: string]: unknown;
                };
                content: {
                    readonly "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    readonly get_run_activity_api_v1_runs__run_id__activity_get: {
        readonly parameters: {
            readonly query?: never;
            readonly header?: never;
            readonly path: {
                readonly run_id: string;
            };
            readonly cookie?: never;
        };
        readonly requestBody?: never;
        readonly responses: {
            /** @description Successful Response */
            readonly 200: {
                headers: {
                    readonly [name: string]: unknown;
                };
                content: {
                    readonly "application/json": readonly components["schemas"]["RunActivityResponse"][];
                };
            };
            /** @description Validation Error */
            readonly 422: {
                headers: {
                    readonly [name: string]: unknown;
                };
                content: {
                    readonly "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    readonly cancel_run_api_v1_runs__run_id__cancel_post: {
        readonly parameters: {
            readonly query?: never;
            readonly header?: never;
            readonly path: {
                readonly run_id: string;
            };
            readonly cookie?: never;
        };
        readonly requestBody?: never;
        readonly responses: {
            /** @description Successful Response */
            readonly 200: {
                headers: {
                    readonly [name: string]: unknown;
                };
                content: {
                    readonly "application/json": components["schemas"]["RunResponse"];
                };
            };
            /** @description Validation Error */
            readonly 422: {
                headers: {
                    readonly [name: string]: unknown;
                };
                content: {
                    readonly "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    readonly ready_health_ready_get: {
        readonly parameters: {
            readonly query?: never;
            readonly header?: never;
            readonly path?: never;
            readonly cookie?: never;
        };
        readonly requestBody?: never;
        readonly responses: {
            /** @description Successful Response */
            readonly 200: {
                headers: {
                    readonly [name: string]: unknown;
                };
                content: {
                    readonly "application/json": {
                        readonly [key: string]: string;
                    };
                };
            };
        };
    };
    readonly apollo_webhook_webhooks_apollo__capability_token__post: {
        readonly parameters: {
            readonly query?: never;
            readonly header?: never;
            readonly path: {
                readonly capability_token: string;
            };
            readonly cookie?: never;
        };
        readonly requestBody?: never;
        readonly responses: {
            /** @description Successful Response */
            readonly 202: {
                headers: {
                    readonly [name: string]: unknown;
                };
                content: {
                    readonly "application/json": {
                        readonly [key: string]: string;
                    };
                };
            };
            /** @description Validation Error */
            readonly 422: {
                headers: {
                    readonly [name: string]: unknown;
                };
                content: {
                    readonly "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
}
