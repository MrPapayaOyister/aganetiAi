// Aria's visual identity is now a particle sphere. This module re-exports
// ParticleSphere under the historical OrbAnimation name + OrbMode type so all
// existing call sites (MessageBubble, AssistantPage, LoginPage) keep working.
export { ParticleSphere as OrbAnimation, default, type OrbMode } from './ParticleSphere'
