# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

Primary users are individual job seekers preparing interviews, including Chinese-speaking candidates who work locally with resumes, job descriptions, project material, and interview answers. They need to organize truthful evidence and practice answers without mixing casual chat context into the interview context. This audience is inferred from the product definition and user brief; it has not yet been validated through user research.

## Product Purpose

Nico lets a job seeker discuss a target role, turn real experience into traceable interview evidence, complete an independent mock interview, review evidence backed feedback, and practice the weakest answer again. Success means the user can explain what evidence supports an answer, where it came from, and what to improve next.

## Positioning

Nico is a two-line job-search workflow with an Agent in each line: a chat assistant for exploration and an interview Agent for assessment. Its differentiating mechanism is the evidence chain linking selected role material, confirmed experience, the original answer, quoted feedback, and the next practice target. A generic chat interface does not preserve that chain.

## Operating Context

The application runs locally by default, with optional account isolation. Chat sessions and interview sessions have separate histories and model context. Job descriptions, resumes, project notes, experience claims, interview answers, feedback, and practice reports are user-owned records. Material enters a model request only after the user explicitly selects or references it.

## Capabilities and Constraints

- Chat and mock interview are separate entries with separate sessions and model context.
- Interview preparation uses user-selected material and user-confirmed experience claims.
- Interviews support one question at a time, correction, skipping, pause, resume, completion, evidence-backed feedback, and single-question practice.
- Chat can discuss a user-specified interview report but does not receive interview history automatically.
- The current product supports local data storage, session search, export, theme persistence, keyboard access, and resumable runs.
- Demo mode is visibly illustrative and must not be presented as a real assessment.
- The product does not rank candidates, conduct real employer screening, or treat model inference as verified personal experience.

## Brand Commitments

- Keep the Nico name and the job evidence-chain product idea.
- The user explicitly prefers the quiet, restrained visual discipline and usable Agent components of `nanobot`.
- Match that level of craft without copying `nanobot` branding, generic capabilities, or page composition wholesale.
- Preserve a recognizable Nico identity centered on role evidence, interview practice, and review.
- The user requested a restrained accent color in place of the earlier strictly grayscale UI. Accent use must remain limited to primary action, focus, and meaningful state.

## Evidence on Hand

The repository contains the implemented chat and interview flows, product definition, demo mode, and three local reference projects. There are no approved customer testimonials, outcome metrics, benchmark claims, or production user research. The interface must not invent them.

## Product Principles

1. The evidence chain must remain visible enough to orient the user and quiet enough to stay behind the current task.
2. The composer and the current conversation are the working surface; onboarding content disappears once the thread begins.
3. Distinguish user facts, model inference, and assessment feedback without presenting any one as the others.
4. Make the next useful action obvious while leaving every consequential read or write under user control.
5. Adopt familiar Agent interaction patterns where they reduce learning cost, and reserve product-specific expression for the evidence workflow.

## Accessibility & Inclusion

The Web interface must work with keyboard navigation and visible focus, maintain readable contrast in light and dark themes, expose accessible labels for icon controls, support reduced motion, and keep touch targets usable on narrow screens. Chinese text needs a comfortable line height and clear hierarchy without relying on color alone.
