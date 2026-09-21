"""DEV-784: default-MainActor isolation — the implicit form of DEV-753."""
from coding_model_autonomous import swift_prechecks as sp
from coding_model_autonomous import swift_rules as sr
from coding_model_autonomous import executor

# Run 50's synthesis repair, trimmed: a Combine sink calling an instance
# method of the same type with no hop.
POSITIVE = """import AVFoundation
import Combine

final class AudioManager {
    private var configurationChangeObserver: AnyCancellable?
    private let lifecycle = AudioLifecycle()

    func executeAction(_ action: AudioAction) {
        _ = action
    }

    func setupConfigurationChangeObserver() {
        configurationChangeObserver = NotificationCenter.default.publisher(for: NSNotification.Name.AVAudioEngineConfigurationChange)
            .receive(on: DispatchQueue.main)
            .sink { [weak self] _ in
                guard let self else { return }
                let action = self.lifecycle.handle(.configurationChanged)
                self.executeAction(action)
            }
    }
}
"""

# Run 51's delivered AudioManager, trimmed: the same shape with the hop.
NEGATIVE = """import AVFoundation

final class AudioManager {
    func handleExternalEvent(_ event: AudioEvent) { _ = event }

    private func registerObservers() {
        NotificationCenter.default.addObserver(forName: NSNotification.Name.AVAudioEngineConfigurationChange, object: nil, queue: nil) { [weak self] _ in
            Task { @MainActor in
                self?.handleExternalEvent(.configurationChanged)
            }
        }
    }
}
"""


def test_run_50_shape_is_flagged_under_default_main_actor():
    v = sp.nonisolated_closure_calls_isolated_method(
        [("ElectricSheep/AudioManager.swift", POSITIVE)], "MainActor")
    assert [(x.kind, x.line) for x in v] == [("nonisolated_closure_isolated_call", 18)]
    assert "'executeAction()'" in v[0].message and "Task { @MainActor in" in v[0].message


def test_the_hop_clears_it_and_the_flag_gates_it():
    assert sp.nonisolated_closure_calls_isolated_method(
        [("ElectricSheep/AudioManager.swift", NEGATIVE)], "MainActor") == []
    assert sp.nonisolated_closure_calls_isolated_method(
        [("ElectricSheep/AudioManager.swift", POSITIVE)], None) == []
    assert sp.nonisolated_closure_calls_isolated_method(
        [("ElectricSheepTests/AudioManagerTests.swift", POSITIVE)], "MainActor") == []


def test_harness_wiring_and_the_swiftc_shaped_line():
    res = sp.run_swift_prechecks([("ElectricSheep/AudioManager.swift", POSITIVE)],
                                 default_isolation="MainActor")
    assert any(x.kind == "nonisolated_closure_isolated_call" for x in res.violations)
    assert res.violations[0].error_line().startswith("ElectricSheep/AudioManager.swift:18:1: error: call to main actor-isolated")
    assert sp.run_swift_prechecks([("ElectricSheep/AudioManager.swift", POSITIVE)]).violations == []


def test_hint_names_both_fixes_and_rule_renders_only_for_main_actor():
    hint = sr.fix_hint("call to main actor-isolated instance method 'stopObservingNotifications()' in a synchronous nonisolated context")
    assert "Task { @MainActor in" in hint and "ENCLOSING declaration" in hint
    assert sr.render_default_isolation_rule("MainActor").startswith("## This target is default-isolated")
    assert sr.render_default_isolation_rule(None) == ""
    assert sr.render_default_isolation_rule("nonisolated") == ""


def test_standing_rules_reach_both_prompts_only_when_given():
    rule = sr.render_default_isolation_rule("MainActor")
    with_rule = executor.build_implementer_message("# spec", "# design", standing_rules=rule)
    without = executor.build_implementer_message("# spec", "# design")
    assert "default-isolated to the main actor" in with_rule[-1]["content"]
    assert "default-isolated" not in without[-1]["content"]
    arch = executor.build_architect_message("# spec", standing_rules=rule)
    assert "default-isolated to the main actor" in arch[-1]["content"]
