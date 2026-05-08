// Shared helpers for the PostHog snippet so the Starlight pages
// (configured via astro.config.mjs's `head`) and the custom landing page
// at src/pages/index.astro emit identical bootstraps.
//
// Both call sites must pass the same env vars (PUBLIC_POSTHOG_KEY,
// PUBLIC_POSTHOG_HOST). When the key is absent the helpers produce no
// script — the build is unaffected.

const POSTHOG_BOOTSTRAP = `!function(t,e){var o,n,p,r;e.__SV||(window.posthog=e,e._i=[],e.init=function(i,s,a){function g(t,e){var o=e.split(".");2==o.length&&(t=t[o[0]],e=o[1]),t[e]=function(){t.push([e].concat(Array.prototype.slice.call(arguments,0)))}}(p=t.createElement("script")).type="text/javascript",p.crossOrigin="anonymous",p.async=!0,p.src=s.api_host.replace(".i.posthog.com","-assets.i.posthog.com")+"/static/array.js",(r=t.getElementsByTagName("script")[0]).parentNode.insertBefore(p,r);var u=e;for(void 0!==a?u=e[a]=[]:a="posthog",u.people=u.people||[],u.toString=function(t){var e="posthog";return"posthog"!==a&&(e+="."+a),t||(e+=" (stub)"),e},u.people.toString=function(){return u.toString(1)+".people (stub)"},o="init capture register register_once register_for_session unregister unregister_for_session getFeatureFlag getFeatureFlagPayload isFeatureEnabled reloadFeatureFlags updateEarlyAccessFeatureEnrollment getEarlyAccessFeatures on onFeatureFlags onSessionId getSurveys getActiveMatchingSurveys renderSurvey canRenderSurvey identify setPersonProperties group resetGroups setPersonPropertiesForFlags resetPersonPropertiesForFlags setGroupPropertiesForFlags resetGroupPropertiesForFlags reset get_distinct_id getGroups get_session_id get_session_replay_url alias set_config startSessionRecording stopSessionRecording sessionRecordingStarted captureException loadToolbar get_property getSessionProperty createPersonProfile opt_in_capturing opt_out_capturing has_opted_in_capturing has_opted_out_capturing clear_opt_in_out_capturing debug getPageViewId captureTraceFeedback captureTraceMetric".split(" "),n=0;n<o.length;n++)g(u,o[n]);e._i.push([i,s,a])},e.__SV=1)}(document,window.posthog||[]);`;

const DEFAULT_POSTHOG_HOST = 'https://us.i.posthog.com';

/**
 * Render the inline-script body for PostHog. Returns null when no key is
 * provided so callers can omit the `<script>` tag entirely.
 *
 * @param {{ key?: string, host?: string }} options
 * @returns {string | null}
 */
export function posthogScriptContent({ key, host }) {
  if (!key) return null;
  const apiHost = host ?? DEFAULT_POSTHOG_HOST;
  return `${POSTHOG_BOOTSTRAP}
posthog.init(${JSON.stringify(key)}, { api_host: ${JSON.stringify(apiHost)}, defaults: '2025-05-24' });`;
}

/**
 * Build the `head` array Starlight expects. Empty when no key is set.
 *
 * @param {{ key?: string, host?: string }} options
 * @returns {Array<{ tag: 'script', content: string }>}
 */
export function posthogHeadEntries({ key, host }) {
  const content = posthogScriptContent({ key, host });
  return content ? [{ tag: 'script', content }] : [];
}
