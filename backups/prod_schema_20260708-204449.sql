--
-- PostgreSQL database dump
--

\restrict ecesN0wlMRwyMYeai4w8piBdX8CSGklazem5xyY5jHixW2MK3m9l8lvp53v5rHf

-- Dumped from database version 15.18
-- Dumped by pg_dump version 15.18

SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', '', false);
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;

--
-- Name: public; Type: SCHEMA; Schema: -; Owner: aganeti
--

-- *not* creating schema, since initdb creates it


ALTER SCHEMA public OWNER TO aganeti;

SET default_tablespace = '';

SET default_table_access_method = heap;

--
-- Name: agent_messages; Type: TABLE; Schema: public; Owner: aganeti
--

CREATE TABLE public.agent_messages (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    org_id uuid NOT NULL,
    user_id uuid,
    from_agent text NOT NULL,
    to_agent text NOT NULL,
    type text DEFAULT 'message'::text NOT NULL,
    payload jsonb DEFAULT '{}'::jsonb NOT NULL,
    status text DEFAULT 'pending'::text NOT NULL,
    read_at timestamp with time zone,
    resolved_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    deleted_at timestamp with time zone
);


ALTER TABLE public.agent_messages OWNER TO aganeti;

--
-- Name: agent_permissions; Type: TABLE; Schema: public; Owner: aganeti
--

CREATE TABLE public.agent_permissions (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    org_id uuid NOT NULL,
    agent_id uuid NOT NULL,
    permission text NOT NULL,
    is_outbound boolean DEFAULT false NOT NULL,
    granted_by uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    deleted_at timestamp with time zone
);


ALTER TABLE public.agent_permissions OWNER TO aganeti;

--
-- Name: agent_runs; Type: TABLE; Schema: public; Owner: aganeti
--

CREATE TABLE public.agent_runs (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    org_id uuid NOT NULL,
    user_id uuid NOT NULL,
    agent_id uuid NOT NULL,
    parent_run_id uuid,
    trigger text,
    goal text,
    status text DEFAULT 'queued'::text NOT NULL,
    state jsonb DEFAULT '{}'::jsonb NOT NULL,
    model_key text,
    tokens_in integer DEFAULT 0 NOT NULL,
    tokens_out integer DEFAULT 0 NOT NULL,
    cost_micros bigint DEFAULT 0 NOT NULL,
    error text,
    started_at timestamp with time zone,
    finished_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    deleted_at timestamp with time zone
);


ALTER TABLE public.agent_runs OWNER TO aganeti;

--
-- Name: agents; Type: TABLE; Schema: public; Owner: aganeti
--

CREATE TABLE public.agents (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    org_id uuid NOT NULL,
    user_id uuid NOT NULL,
    kind text NOT NULL,
    template_key text,
    name text NOT NULL,
    persona text,
    system_prompt text NOT NULL,
    model_key text,
    fallback_models jsonb DEFAULT '[]'::jsonb NOT NULL,
    status text DEFAULT 'active'::text NOT NULL,
    config jsonb DEFAULT '{}'::jsonb NOT NULL,
    department_id uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    deleted_at timestamp with time zone
);


ALTER TABLE public.agents OWNER TO aganeti;

--
-- Name: approvals; Type: TABLE; Schema: public; Owner: aganeti
--

CREATE TABLE public.approvals (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    org_id uuid NOT NULL,
    user_id uuid NOT NULL,
    agent_id uuid,
    run_id uuid,
    tool_key text NOT NULL,
    action_type text,
    payload jsonb DEFAULT '{}'::jsonb NOT NULL,
    preview text,
    status text DEFAULT 'pending'::text NOT NULL,
    result text,
    decided_by uuid,
    decided_at timestamp with time zone,
    expires_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    deleted_at timestamp with time zone
);


ALTER TABLE public.approvals OWNER TO aganeti;

--
-- Name: contacts; Type: TABLE; Schema: public; Owner: aganeti
--

CREATE TABLE public.contacts (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    org_id uuid NOT NULL,
    user_id uuid,
    full_name text NOT NULL,
    email text,
    nickname text,
    department text,
    role text,
    is_agent boolean DEFAULT false NOT NULL,
    notes text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    deleted_at timestamp with time zone
);


ALTER TABLE public.contacts OWNER TO aganeti;

--
-- Name: delegations; Type: TABLE; Schema: public; Owner: aganeti
--

CREATE TABLE public.delegations (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    org_id uuid NOT NULL,
    user_id uuid NOT NULL,
    from_agent_id uuid,
    to_agent_id uuid,
    task text NOT NULL,
    status text DEFAULT 'pending'::text NOT NULL,
    result text,
    error text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    deleted_at timestamp with time zone
);


ALTER TABLE public.delegations OWNER TO aganeti;

--
-- Name: departments; Type: TABLE; Schema: public; Owner: aganeti
--

CREATE TABLE public.departments (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    org_id uuid NOT NULL,
    name text NOT NULL,
    parent_id uuid,
    head_user_id uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    deleted_at timestamp with time zone
);


ALTER TABLE public.departments OWNER TO aganeti;

--
-- Name: designations; Type: TABLE; Schema: public; Owner: aganeti
--

CREATE TABLE public.designations (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    org_id uuid NOT NULL,
    title text NOT NULL,
    level integer DEFAULT 1 NOT NULL,
    scopes jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    deleted_at timestamp with time zone
);


ALTER TABLE public.designations OWNER TO aganeti;

--
-- Name: employee_profiles; Type: TABLE; Schema: public; Owner: aganeti
--

CREATE TABLE public.employee_profiles (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    org_id uuid NOT NULL,
    user_id uuid NOT NULL,
    job_description text,
    responsibilities jsonb DEFAULT '{}'::jsonb NOT NULL,
    working_hours jsonb DEFAULT '{}'::jsonb NOT NULL,
    seed_prompt text,
    onboarded_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    deleted_at timestamp with time zone
);


ALTER TABLE public.employee_profiles OWNER TO aganeti;

--
-- Name: events; Type: TABLE; Schema: public; Owner: aganeti
--

CREATE TABLE public.events (
    id bigint NOT NULL,
    ts timestamp with time zone DEFAULT now() NOT NULL,
    org_id uuid,
    user_id uuid,
    agent_id uuid,
    run_id uuid,
    kind text NOT NULL,
    name text,
    success boolean,
    duration_ms integer,
    cost_micros bigint,
    meta jsonb DEFAULT '{}'::jsonb NOT NULL
);


ALTER TABLE public.events OWNER TO aganeti;

--
-- Name: events_id_seq; Type: SEQUENCE; Schema: public; Owner: aganeti
--

CREATE SEQUENCE public.events_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER TABLE public.events_id_seq OWNER TO aganeti;

--
-- Name: events_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: aganeti
--

ALTER SEQUENCE public.events_id_seq OWNED BY public.events.id;


--
-- Name: initiatives; Type: TABLE; Schema: public; Owner: aganeti
--

CREATE TABLE public.initiatives (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    org_id uuid NOT NULL,
    user_id uuid NOT NULL,
    category text NOT NULL,
    title text NOT NULL,
    body text NOT NULL,
    dedup_key text,
    status text DEFAULT 'pending'::text NOT NULL,
    acted_at timestamp with time zone,
    meta jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    deleted_at timestamp with time zone
);


ALTER TABLE public.initiatives OWNER TO aganeti;

--
-- Name: interactions; Type: TABLE; Schema: public; Owner: aganeti
--

CREATE TABLE public.interactions (
    id bigint NOT NULL,
    org_id uuid,
    user_id uuid,
    contact_email text,
    contact_name text,
    channel text NOT NULL,
    direction text,
    ts timestamp with time zone DEFAULT now() NOT NULL,
    subject text,
    ref_id text
);


ALTER TABLE public.interactions OWNER TO aganeti;

--
-- Name: interactions_id_seq; Type: SEQUENCE; Schema: public; Owner: aganeti
--

CREATE SEQUENCE public.interactions_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER TABLE public.interactions_id_seq OWNER TO aganeti;

--
-- Name: interactions_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: aganeti
--

ALTER SEQUENCE public.interactions_id_seq OWNED BY public.interactions.id;


--
-- Name: opportunities; Type: TABLE; Schema: public; Owner: aganeti
--

CREATE TABLE public.opportunities (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    org_id uuid NOT NULL,
    user_id uuid NOT NULL,
    title text NOT NULL,
    counterparty text,
    value_amount bigint DEFAULT 0 NOT NULL,
    currency text DEFAULT 'USD'::text NOT NULL,
    stage text DEFAULT 'prospect'::text NOT NULL,
    probability integer DEFAULT 50 NOT NULL,
    expected_close timestamp with time zone,
    notes text,
    status text DEFAULT 'open'::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    deleted_at timestamp with time zone
);


ALTER TABLE public.opportunities OWNER TO aganeti;

--
-- Name: organizations; Type: TABLE; Schema: public; Owner: aganeti
--

CREATE TABLE public.organizations (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    name text NOT NULL,
    slug text NOT NULL,
    settings jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    deleted_at timestamp with time zone
);


ALTER TABLE public.organizations OWNER TO aganeti;

--
-- Name: schedules; Type: TABLE; Schema: public; Owner: aganeti
--

CREATE TABLE public.schedules (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    org_id uuid NOT NULL,
    user_id uuid NOT NULL,
    label text NOT NULL,
    cron_expression text NOT NULL,
    action_type text NOT NULL,
    action_payload jsonb DEFAULT '{}'::jsonb NOT NULL,
    is_active boolean DEFAULT true NOT NULL,
    agent_id uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    deleted_at timestamp with time zone
);


ALTER TABLE public.schedules OWNER TO aganeti;

--
-- Name: tasks; Type: TABLE; Schema: public; Owner: aganeti
--

CREATE TABLE public.tasks (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    org_id uuid NOT NULL,
    user_id uuid NOT NULL,
    assignee_id uuid,
    title text NOT NULL,
    source text,
    status text DEFAULT 'pending'::text NOT NULL,
    priority text DEFAULT 'medium'::text NOT NULL,
    due_date timestamp with time zone,
    notes text,
    agent_id uuid,
    reminder_sent boolean DEFAULT false NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    deleted_at timestamp with time zone
);


ALTER TABLE public.tasks OWNER TO aganeti;

--
-- Name: users; Type: TABLE; Schema: public; Owner: aganeti
--

CREATE TABLE public.users (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    org_id uuid NOT NULL,
    supabase_uid text NOT NULL,
    email text NOT NULL,
    full_name text,
    department_id uuid,
    designation_id uuid,
    role text DEFAULT 'employee'::text NOT NULL,
    manager_id uuid,
    primary_agent_id uuid,
    locale text DEFAULT 'en'::text NOT NULL,
    status text DEFAULT 'active'::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    deleted_at timestamp with time zone,
    settings jsonb DEFAULT '{}'::jsonb
);


ALTER TABLE public.users OWNER TO aganeti;

--
-- Name: events id; Type: DEFAULT; Schema: public; Owner: aganeti
--

ALTER TABLE ONLY public.events ALTER COLUMN id SET DEFAULT nextval('public.events_id_seq'::regclass);


--
-- Name: interactions id; Type: DEFAULT; Schema: public; Owner: aganeti
--

ALTER TABLE ONLY public.interactions ALTER COLUMN id SET DEFAULT nextval('public.interactions_id_seq'::regclass);


--
-- Name: agent_messages agent_messages_pkey; Type: CONSTRAINT; Schema: public; Owner: aganeti
--

ALTER TABLE ONLY public.agent_messages
    ADD CONSTRAINT agent_messages_pkey PRIMARY KEY (id);


--
-- Name: agent_permissions agent_permissions_pkey; Type: CONSTRAINT; Schema: public; Owner: aganeti
--

ALTER TABLE ONLY public.agent_permissions
    ADD CONSTRAINT agent_permissions_pkey PRIMARY KEY (id);


--
-- Name: agent_runs agent_runs_pkey; Type: CONSTRAINT; Schema: public; Owner: aganeti
--

ALTER TABLE ONLY public.agent_runs
    ADD CONSTRAINT agent_runs_pkey PRIMARY KEY (id);


--
-- Name: agents agents_pkey; Type: CONSTRAINT; Schema: public; Owner: aganeti
--

ALTER TABLE ONLY public.agents
    ADD CONSTRAINT agents_pkey PRIMARY KEY (id);


--
-- Name: approvals approvals_pkey; Type: CONSTRAINT; Schema: public; Owner: aganeti
--

ALTER TABLE ONLY public.approvals
    ADD CONSTRAINT approvals_pkey PRIMARY KEY (id);


--
-- Name: contacts contacts_pkey; Type: CONSTRAINT; Schema: public; Owner: aganeti
--

ALTER TABLE ONLY public.contacts
    ADD CONSTRAINT contacts_pkey PRIMARY KEY (id);


--
-- Name: delegations delegations_pkey; Type: CONSTRAINT; Schema: public; Owner: aganeti
--

ALTER TABLE ONLY public.delegations
    ADD CONSTRAINT delegations_pkey PRIMARY KEY (id);


--
-- Name: departments departments_pkey; Type: CONSTRAINT; Schema: public; Owner: aganeti
--

ALTER TABLE ONLY public.departments
    ADD CONSTRAINT departments_pkey PRIMARY KEY (id);


--
-- Name: designations designations_pkey; Type: CONSTRAINT; Schema: public; Owner: aganeti
--

ALTER TABLE ONLY public.designations
    ADD CONSTRAINT designations_pkey PRIMARY KEY (id);


--
-- Name: employee_profiles employee_profiles_pkey; Type: CONSTRAINT; Schema: public; Owner: aganeti
--

ALTER TABLE ONLY public.employee_profiles
    ADD CONSTRAINT employee_profiles_pkey PRIMARY KEY (id);


--
-- Name: events events_pkey; Type: CONSTRAINT; Schema: public; Owner: aganeti
--

ALTER TABLE ONLY public.events
    ADD CONSTRAINT events_pkey PRIMARY KEY (id);


--
-- Name: initiatives initiatives_pkey; Type: CONSTRAINT; Schema: public; Owner: aganeti
--

ALTER TABLE ONLY public.initiatives
    ADD CONSTRAINT initiatives_pkey PRIMARY KEY (id);


--
-- Name: interactions interactions_pkey; Type: CONSTRAINT; Schema: public; Owner: aganeti
--

ALTER TABLE ONLY public.interactions
    ADD CONSTRAINT interactions_pkey PRIMARY KEY (id);


--
-- Name: opportunities opportunities_pkey; Type: CONSTRAINT; Schema: public; Owner: aganeti
--

ALTER TABLE ONLY public.opportunities
    ADD CONSTRAINT opportunities_pkey PRIMARY KEY (id);


--
-- Name: organizations organizations_pkey; Type: CONSTRAINT; Schema: public; Owner: aganeti
--

ALTER TABLE ONLY public.organizations
    ADD CONSTRAINT organizations_pkey PRIMARY KEY (id);


--
-- Name: organizations organizations_slug_key; Type: CONSTRAINT; Schema: public; Owner: aganeti
--

ALTER TABLE ONLY public.organizations
    ADD CONSTRAINT organizations_slug_key UNIQUE (slug);


--
-- Name: schedules schedules_pkey; Type: CONSTRAINT; Schema: public; Owner: aganeti
--

ALTER TABLE ONLY public.schedules
    ADD CONSTRAINT schedules_pkey PRIMARY KEY (id);


--
-- Name: tasks tasks_pkey; Type: CONSTRAINT; Schema: public; Owner: aganeti
--

ALTER TABLE ONLY public.tasks
    ADD CONSTRAINT tasks_pkey PRIMARY KEY (id);


--
-- Name: agent_permissions uq_agentperm; Type: CONSTRAINT; Schema: public; Owner: aganeti
--

ALTER TABLE ONLY public.agent_permissions
    ADD CONSTRAINT uq_agentperm UNIQUE (agent_id, permission);


--
-- Name: departments uq_dept_org_name; Type: CONSTRAINT; Schema: public; Owner: aganeti
--

ALTER TABLE ONLY public.departments
    ADD CONSTRAINT uq_dept_org_name UNIQUE (org_id, name);


--
-- Name: designations uq_desig_org_title; Type: CONSTRAINT; Schema: public; Owner: aganeti
--

ALTER TABLE ONLY public.designations
    ADD CONSTRAINT uq_desig_org_title UNIQUE (org_id, title);


--
-- Name: interactions uq_interaction_ref; Type: CONSTRAINT; Schema: public; Owner: aganeti
--

ALTER TABLE ONLY public.interactions
    ADD CONSTRAINT uq_interaction_ref UNIQUE (user_id, channel, ref_id);


--
-- Name: employee_profiles uq_profile_user; Type: CONSTRAINT; Schema: public; Owner: aganeti
--

ALTER TABLE ONLY public.employee_profiles
    ADD CONSTRAINT uq_profile_user UNIQUE (user_id);


--
-- Name: users users_pkey; Type: CONSTRAINT; Schema: public; Owner: aganeti
--

ALTER TABLE ONLY public.users
    ADD CONSTRAINT users_pkey PRIMARY KEY (id);


--
-- Name: users users_supabase_uid_key; Type: CONSTRAINT; Schema: public; Owner: aganeti
--

ALTER TABLE ONLY public.users
    ADD CONSTRAINT users_supabase_uid_key UNIQUE (supabase_uid);


--
-- Name: ix_agent_messages_org_id; Type: INDEX; Schema: public; Owner: aganeti
--

CREATE INDEX ix_agent_messages_org_id ON public.agent_messages USING btree (org_id);


--
-- Name: ix_agent_permissions_agent_id; Type: INDEX; Schema: public; Owner: aganeti
--

CREATE INDEX ix_agent_permissions_agent_id ON public.agent_permissions USING btree (agent_id);


--
-- Name: ix_agent_permissions_org_id; Type: INDEX; Schema: public; Owner: aganeti
--

CREATE INDEX ix_agent_permissions_org_id ON public.agent_permissions USING btree (org_id);


--
-- Name: ix_agent_runs_agent_id; Type: INDEX; Schema: public; Owner: aganeti
--

CREATE INDEX ix_agent_runs_agent_id ON public.agent_runs USING btree (agent_id);


--
-- Name: ix_agent_runs_org_id; Type: INDEX; Schema: public; Owner: aganeti
--

CREATE INDEX ix_agent_runs_org_id ON public.agent_runs USING btree (org_id);


--
-- Name: ix_agent_runs_user_id; Type: INDEX; Schema: public; Owner: aganeti
--

CREATE INDEX ix_agent_runs_user_id ON public.agent_runs USING btree (user_id);


--
-- Name: ix_agents_org_id; Type: INDEX; Schema: public; Owner: aganeti
--

CREATE INDEX ix_agents_org_id ON public.agents USING btree (org_id);


--
-- Name: ix_agents_user_id; Type: INDEX; Schema: public; Owner: aganeti
--

CREATE INDEX ix_agents_user_id ON public.agents USING btree (user_id);


--
-- Name: ix_approvals_org_id; Type: INDEX; Schema: public; Owner: aganeti
--

CREATE INDEX ix_approvals_org_id ON public.approvals USING btree (org_id);


--
-- Name: ix_approvals_user_id; Type: INDEX; Schema: public; Owner: aganeti
--

CREATE INDEX ix_approvals_user_id ON public.approvals USING btree (user_id);


--
-- Name: ix_contacts_org_id; Type: INDEX; Schema: public; Owner: aganeti
--

CREATE INDEX ix_contacts_org_id ON public.contacts USING btree (org_id);


--
-- Name: ix_delegations_org_id; Type: INDEX; Schema: public; Owner: aganeti
--

CREATE INDEX ix_delegations_org_id ON public.delegations USING btree (org_id);


--
-- Name: ix_delegations_user_id; Type: INDEX; Schema: public; Owner: aganeti
--

CREATE INDEX ix_delegations_user_id ON public.delegations USING btree (user_id);


--
-- Name: ix_departments_org_id; Type: INDEX; Schema: public; Owner: aganeti
--

CREATE INDEX ix_departments_org_id ON public.departments USING btree (org_id);


--
-- Name: ix_designations_org_id; Type: INDEX; Schema: public; Owner: aganeti
--

CREATE INDEX ix_designations_org_id ON public.designations USING btree (org_id);


--
-- Name: ix_employee_profiles_org_id; Type: INDEX; Schema: public; Owner: aganeti
--

CREATE INDEX ix_employee_profiles_org_id ON public.employee_profiles USING btree (org_id);


--
-- Name: ix_employee_profiles_user_id; Type: INDEX; Schema: public; Owner: aganeti
--

CREATE INDEX ix_employee_profiles_user_id ON public.employee_profiles USING btree (user_id);


--
-- Name: ix_events_org_id; Type: INDEX; Schema: public; Owner: aganeti
--

CREATE INDEX ix_events_org_id ON public.events USING btree (org_id);


--
-- Name: ix_events_ts; Type: INDEX; Schema: public; Owner: aganeti
--

CREATE INDEX ix_events_ts ON public.events USING btree (ts);


--
-- Name: ix_events_user_id; Type: INDEX; Schema: public; Owner: aganeti
--

CREATE INDEX ix_events_user_id ON public.events USING btree (user_id);


--
-- Name: ix_initiatives_org_id; Type: INDEX; Schema: public; Owner: aganeti
--

CREATE INDEX ix_initiatives_org_id ON public.initiatives USING btree (org_id);


--
-- Name: ix_initiatives_user_id; Type: INDEX; Schema: public; Owner: aganeti
--

CREATE INDEX ix_initiatives_user_id ON public.initiatives USING btree (user_id);


--
-- Name: ix_interactions_contact_email; Type: INDEX; Schema: public; Owner: aganeti
--

CREATE INDEX ix_interactions_contact_email ON public.interactions USING btree (contact_email);


--
-- Name: ix_interactions_org_id; Type: INDEX; Schema: public; Owner: aganeti
--

CREATE INDEX ix_interactions_org_id ON public.interactions USING btree (org_id);


--
-- Name: ix_interactions_ts; Type: INDEX; Schema: public; Owner: aganeti
--

CREATE INDEX ix_interactions_ts ON public.interactions USING btree (ts);


--
-- Name: ix_interactions_user_id; Type: INDEX; Schema: public; Owner: aganeti
--

CREATE INDEX ix_interactions_user_id ON public.interactions USING btree (user_id);


--
-- Name: ix_opportunities_org_id; Type: INDEX; Schema: public; Owner: aganeti
--

CREATE INDEX ix_opportunities_org_id ON public.opportunities USING btree (org_id);


--
-- Name: ix_opportunities_user_id; Type: INDEX; Schema: public; Owner: aganeti
--

CREATE INDEX ix_opportunities_user_id ON public.opportunities USING btree (user_id);


--
-- Name: ix_schedules_org_id; Type: INDEX; Schema: public; Owner: aganeti
--

CREATE INDEX ix_schedules_org_id ON public.schedules USING btree (org_id);


--
-- Name: ix_schedules_user_id; Type: INDEX; Schema: public; Owner: aganeti
--

CREATE INDEX ix_schedules_user_id ON public.schedules USING btree (user_id);


--
-- Name: ix_tasks_org_id; Type: INDEX; Schema: public; Owner: aganeti
--

CREATE INDEX ix_tasks_org_id ON public.tasks USING btree (org_id);


--
-- Name: ix_tasks_user_id; Type: INDEX; Schema: public; Owner: aganeti
--

CREATE INDEX ix_tasks_user_id ON public.tasks USING btree (user_id);


--
-- Name: ix_users_org_id; Type: INDEX; Schema: public; Owner: aganeti
--

CREATE INDEX ix_users_org_id ON public.users USING btree (org_id);


--
-- Name: uq_one_primary_per_user; Type: INDEX; Schema: public; Owner: aganeti
--

CREATE UNIQUE INDEX uq_one_primary_per_user ON public.agents USING btree (user_id) WHERE ((kind = 'primary'::text) AND (deleted_at IS NULL));


--
-- Name: agent_messages agent_messages_org_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: aganeti
--

ALTER TABLE ONLY public.agent_messages
    ADD CONSTRAINT agent_messages_org_id_fkey FOREIGN KEY (org_id) REFERENCES public.organizations(id) ON DELETE CASCADE;


--
-- Name: agent_messages agent_messages_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: aganeti
--

ALTER TABLE ONLY public.agent_messages
    ADD CONSTRAINT agent_messages_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE SET NULL;


--
-- Name: agent_permissions agent_permissions_agent_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: aganeti
--

ALTER TABLE ONLY public.agent_permissions
    ADD CONSTRAINT agent_permissions_agent_id_fkey FOREIGN KEY (agent_id) REFERENCES public.agents(id) ON DELETE CASCADE;


--
-- Name: agent_permissions agent_permissions_granted_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: aganeti
--

ALTER TABLE ONLY public.agent_permissions
    ADD CONSTRAINT agent_permissions_granted_by_fkey FOREIGN KEY (granted_by) REFERENCES public.users(id) ON DELETE SET NULL;


--
-- Name: agent_permissions agent_permissions_org_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: aganeti
--

ALTER TABLE ONLY public.agent_permissions
    ADD CONSTRAINT agent_permissions_org_id_fkey FOREIGN KEY (org_id) REFERENCES public.organizations(id) ON DELETE CASCADE;


--
-- Name: agent_runs agent_runs_agent_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: aganeti
--

ALTER TABLE ONLY public.agent_runs
    ADD CONSTRAINT agent_runs_agent_id_fkey FOREIGN KEY (agent_id) REFERENCES public.agents(id) ON DELETE CASCADE;


--
-- Name: agent_runs agent_runs_org_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: aganeti
--

ALTER TABLE ONLY public.agent_runs
    ADD CONSTRAINT agent_runs_org_id_fkey FOREIGN KEY (org_id) REFERENCES public.organizations(id) ON DELETE CASCADE;


--
-- Name: agent_runs agent_runs_parent_run_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: aganeti
--

ALTER TABLE ONLY public.agent_runs
    ADD CONSTRAINT agent_runs_parent_run_id_fkey FOREIGN KEY (parent_run_id) REFERENCES public.agent_runs(id) ON DELETE SET NULL;


--
-- Name: agent_runs agent_runs_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: aganeti
--

ALTER TABLE ONLY public.agent_runs
    ADD CONSTRAINT agent_runs_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;


--
-- Name: agents agents_department_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: aganeti
--

ALTER TABLE ONLY public.agents
    ADD CONSTRAINT agents_department_id_fkey FOREIGN KEY (department_id) REFERENCES public.departments(id) ON DELETE SET NULL;


--
-- Name: agents agents_org_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: aganeti
--

ALTER TABLE ONLY public.agents
    ADD CONSTRAINT agents_org_id_fkey FOREIGN KEY (org_id) REFERENCES public.organizations(id) ON DELETE CASCADE;


--
-- Name: agents agents_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: aganeti
--

ALTER TABLE ONLY public.agents
    ADD CONSTRAINT agents_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;


--
-- Name: approvals approvals_agent_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: aganeti
--

ALTER TABLE ONLY public.approvals
    ADD CONSTRAINT approvals_agent_id_fkey FOREIGN KEY (agent_id) REFERENCES public.agents(id) ON DELETE SET NULL;


--
-- Name: approvals approvals_decided_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: aganeti
--

ALTER TABLE ONLY public.approvals
    ADD CONSTRAINT approvals_decided_by_fkey FOREIGN KEY (decided_by) REFERENCES public.users(id) ON DELETE SET NULL;


--
-- Name: approvals approvals_org_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: aganeti
--

ALTER TABLE ONLY public.approvals
    ADD CONSTRAINT approvals_org_id_fkey FOREIGN KEY (org_id) REFERENCES public.organizations(id) ON DELETE CASCADE;


--
-- Name: approvals approvals_run_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: aganeti
--

ALTER TABLE ONLY public.approvals
    ADD CONSTRAINT approvals_run_id_fkey FOREIGN KEY (run_id) REFERENCES public.agent_runs(id) ON DELETE CASCADE;


--
-- Name: approvals approvals_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: aganeti
--

ALTER TABLE ONLY public.approvals
    ADD CONSTRAINT approvals_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;


--
-- Name: contacts contacts_org_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: aganeti
--

ALTER TABLE ONLY public.contacts
    ADD CONSTRAINT contacts_org_id_fkey FOREIGN KEY (org_id) REFERENCES public.organizations(id) ON DELETE CASCADE;


--
-- Name: contacts contacts_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: aganeti
--

ALTER TABLE ONLY public.contacts
    ADD CONSTRAINT contacts_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE SET NULL;


--
-- Name: delegations delegations_from_agent_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: aganeti
--

ALTER TABLE ONLY public.delegations
    ADD CONSTRAINT delegations_from_agent_id_fkey FOREIGN KEY (from_agent_id) REFERENCES public.agents(id) ON DELETE SET NULL;


--
-- Name: delegations delegations_org_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: aganeti
--

ALTER TABLE ONLY public.delegations
    ADD CONSTRAINT delegations_org_id_fkey FOREIGN KEY (org_id) REFERENCES public.organizations(id) ON DELETE CASCADE;


--
-- Name: delegations delegations_to_agent_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: aganeti
--

ALTER TABLE ONLY public.delegations
    ADD CONSTRAINT delegations_to_agent_id_fkey FOREIGN KEY (to_agent_id) REFERENCES public.agents(id) ON DELETE SET NULL;


--
-- Name: delegations delegations_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: aganeti
--

ALTER TABLE ONLY public.delegations
    ADD CONSTRAINT delegations_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;


--
-- Name: departments departments_org_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: aganeti
--

ALTER TABLE ONLY public.departments
    ADD CONSTRAINT departments_org_id_fkey FOREIGN KEY (org_id) REFERENCES public.organizations(id) ON DELETE CASCADE;


--
-- Name: departments departments_parent_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: aganeti
--

ALTER TABLE ONLY public.departments
    ADD CONSTRAINT departments_parent_id_fkey FOREIGN KEY (parent_id) REFERENCES public.departments(id) ON DELETE SET NULL;


--
-- Name: designations designations_org_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: aganeti
--

ALTER TABLE ONLY public.designations
    ADD CONSTRAINT designations_org_id_fkey FOREIGN KEY (org_id) REFERENCES public.organizations(id) ON DELETE CASCADE;


--
-- Name: employee_profiles employee_profiles_org_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: aganeti
--

ALTER TABLE ONLY public.employee_profiles
    ADD CONSTRAINT employee_profiles_org_id_fkey FOREIGN KEY (org_id) REFERENCES public.organizations(id) ON DELETE CASCADE;


--
-- Name: employee_profiles employee_profiles_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: aganeti
--

ALTER TABLE ONLY public.employee_profiles
    ADD CONSTRAINT employee_profiles_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;


--
-- Name: initiatives initiatives_org_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: aganeti
--

ALTER TABLE ONLY public.initiatives
    ADD CONSTRAINT initiatives_org_id_fkey FOREIGN KEY (org_id) REFERENCES public.organizations(id) ON DELETE CASCADE;


--
-- Name: initiatives initiatives_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: aganeti
--

ALTER TABLE ONLY public.initiatives
    ADD CONSTRAINT initiatives_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;


--
-- Name: opportunities opportunities_org_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: aganeti
--

ALTER TABLE ONLY public.opportunities
    ADD CONSTRAINT opportunities_org_id_fkey FOREIGN KEY (org_id) REFERENCES public.organizations(id) ON DELETE CASCADE;


--
-- Name: opportunities opportunities_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: aganeti
--

ALTER TABLE ONLY public.opportunities
    ADD CONSTRAINT opportunities_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;


--
-- Name: schedules schedules_agent_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: aganeti
--

ALTER TABLE ONLY public.schedules
    ADD CONSTRAINT schedules_agent_id_fkey FOREIGN KEY (agent_id) REFERENCES public.agents(id) ON DELETE SET NULL;


--
-- Name: schedules schedules_org_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: aganeti
--

ALTER TABLE ONLY public.schedules
    ADD CONSTRAINT schedules_org_id_fkey FOREIGN KEY (org_id) REFERENCES public.organizations(id) ON DELETE CASCADE;


--
-- Name: schedules schedules_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: aganeti
--

ALTER TABLE ONLY public.schedules
    ADD CONSTRAINT schedules_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;


--
-- Name: tasks tasks_agent_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: aganeti
--

ALTER TABLE ONLY public.tasks
    ADD CONSTRAINT tasks_agent_id_fkey FOREIGN KEY (agent_id) REFERENCES public.agents(id) ON DELETE SET NULL;


--
-- Name: tasks tasks_assignee_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: aganeti
--

ALTER TABLE ONLY public.tasks
    ADD CONSTRAINT tasks_assignee_id_fkey FOREIGN KEY (assignee_id) REFERENCES public.users(id) ON DELETE SET NULL;


--
-- Name: tasks tasks_org_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: aganeti
--

ALTER TABLE ONLY public.tasks
    ADD CONSTRAINT tasks_org_id_fkey FOREIGN KEY (org_id) REFERENCES public.organizations(id) ON DELETE CASCADE;


--
-- Name: tasks tasks_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: aganeti
--

ALTER TABLE ONLY public.tasks
    ADD CONSTRAINT tasks_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;


--
-- Name: users users_department_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: aganeti
--

ALTER TABLE ONLY public.users
    ADD CONSTRAINT users_department_id_fkey FOREIGN KEY (department_id) REFERENCES public.departments(id) ON DELETE SET NULL;


--
-- Name: users users_designation_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: aganeti
--

ALTER TABLE ONLY public.users
    ADD CONSTRAINT users_designation_id_fkey FOREIGN KEY (designation_id) REFERENCES public.designations(id) ON DELETE SET NULL;


--
-- Name: users users_manager_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: aganeti
--

ALTER TABLE ONLY public.users
    ADD CONSTRAINT users_manager_id_fkey FOREIGN KEY (manager_id) REFERENCES public.users(id) ON DELETE SET NULL;


--
-- Name: users users_org_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: aganeti
--

ALTER TABLE ONLY public.users
    ADD CONSTRAINT users_org_id_fkey FOREIGN KEY (org_id) REFERENCES public.organizations(id) ON DELETE CASCADE;


--
-- PostgreSQL database dump complete
--

\unrestrict ecesN0wlMRwyMYeai4w8piBdX8CSGklazem5xyY5jHixW2MK3m9l8lvp53v5rHf

