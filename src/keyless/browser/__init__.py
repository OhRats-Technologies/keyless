"""Visible persistent Playwright Firefox browser agent with passive gaze-region scrolling."""

from keyless.browser.agent import AgentAction, PlannerFn, SpeakFn, VoiceBrowserAgent
from keyless.browser.planner import ProviderEndpoint, build_browser_planner, endpoint_from_provider
from keyless.browser.profile_sync import (
    KEYLESS_FIREFOX_PROFILE,
    default_playwright_firefox_profile,
    find_default_firefox_profile,
    sync_firefox_profile,
)
from keyless.browser.runtime import BrowserRuntime
from keyless.browser.session import PageObservation, PlaywrightFirefoxSession

__all__ = [
    "AgentAction",
    "BrowserRuntime",
    "KEYLESS_FIREFOX_PROFILE",
    "PageObservation",
    "PlannerFn",
    "PlaywrightFirefoxSession",
    "ProviderEndpoint",
    "SpeakFn",
    "VoiceBrowserAgent",
    "build_browser_planner",
    "default_playwright_firefox_profile",
    "endpoint_from_provider",
    "find_default_firefox_profile",
    "sync_firefox_profile",
]
