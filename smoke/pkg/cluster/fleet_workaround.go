package cluster

import (
	"context"
	"fmt"
	"strings"
	"testing"
	"time"

	fleetv1 "github.com/rancher/fleet/pkg/apis/fleet.cattle.io/v1alpha1"
	appsv1 "k8s.io/api/apps/v1"
	"k8s.io/apimachinery/pkg/types"
	"sigs.k8s.io/e2e-framework/klient/k8s"
	"sigs.k8s.io/e2e-framework/klient/k8s/resources"
	"sigs.k8s.io/e2e-framework/pkg/envconf"
)

// Workaround for fleet-agent's drift watchers getting stuck on "410 Expired"
// after the kube-apiserver restarts while the agent is running (e.g. on a
// cold start). The agent then stops recomputing BundleDeployment status, so
// a bundle stays NotReady even though the resources it reports are ready,
// and the corresponding ManagedChart never becomes ready. Restarting the
// agent recovers it.
//
// The watcher ignores watch.Error events and keeps resuming from the
// expired resourceVersion; see internal/cmd/agent/trigger/watcher.go in
// rancher/fleet.

const (
	fleetAgentNamespace = "cattle-fleet-local-system"
	fleetAgentName      = "fleet-agent"

	// staleStatusGrace is how long a BundleDeployment's Ready=False
	// condition must stay unchanged, while its non-ready resources are all
	// ready, before the status is considered stale.
	staleStatusGrace = 2 * time.Minute
	// fleetAgentRestartCooldown is the minimum time between restarts, so
	// the new agent has time to recompute the status.
	fleetAgentRestartCooldown = 3 * time.Minute
	maxFleetAgentRestarts     = 2
)

type staleFleetAgentWorkaround struct {
	restarts      int
	lastRestartAt time.Time
}

// check restarts fleet-agent if any BundleDeployment status looks stale.
// Errors are logged rather than returned: the workaround must never fail
// the readiness test on its own.
func (w *staleFleetAgentWorkaround) check(ctx context.Context, t *testing.T, cfg *envconf.Config) {
	if w.restarts >= maxFleetAgentRestarts {
		return
	}
	if !w.lastRestartAt.IsZero() && time.Since(w.lastRestartAt) < fleetAgentRestartCooldown {
		return
	}

	r := cfg.Client().Resources()
	stale, err := findStaleBundleDeployments(ctx, r)
	if err != nil {
		t.Logf("fleet-agent workaround: fail to check BundleDeployments: %v", err)
		return
	}
	if len(stale) == 0 {
		return
	}

	t.Logf("fleet-agent workaround: BundleDeployment status looks stale (resources are ready): %s",
		strings.Join(stale, ", "))
	if err := restartFleetAgent(ctx, r); err != nil {
		t.Logf("fleet-agent workaround: fail to restart %s/%s: %v", fleetAgentNamespace, fleetAgentName, err)
		return
	}
	w.restarts++
	w.lastRestartAt = time.Now()
	t.Logf("fleet-agent workaround: restarted %s/%s (%d/%d)",
		fleetAgentNamespace, fleetAgentName, w.restarts, maxFleetAgentRestarts)
}

// findStaleBundleDeployments returns "namespace/name" of BundleDeployments
// whose Ready=False condition has not been updated for staleStatusGrace,
// and whose reported non-ready resources are all ready now.
func findStaleBundleDeployments(ctx context.Context, r *resources.Resources) ([]string, error) {
	var bdList fleetv1.BundleDeploymentList
	if err := r.List(ctx, &bdList); err != nil {
		return nil, err
	}

	var stale []string
	for _, bd := range bdList.Items {
		ok, err := isBundleDeploymentStatusStale(ctx, r, &bd)
		if err != nil {
			return nil, fmt.Errorf("%s/%s: %w", bd.Namespace, bd.Name, err)
		}
		if ok {
			stale = append(stale, fmt.Sprintf("%s/%s", bd.Namespace, bd.Name))
		}
	}
	return stale, nil
}

// isBundleDeploymentStatusStale reports whether bd's NotReady status looks
// left over from a fleet-agent that stopped updating it.
//
// The stuck agent leaves behind a status that is not just old but wrong: it
// names resources as not ready that are ready now. The checks below look for
// exactly that, and treat anything they cannot confirm as not stale, so a
// healthy agent that is still catching up is never restarted.
func isBundleDeploymentStatusStale(ctx context.Context, r *resources.Resources, bd *fleetv1.BundleDeployment) (bool, error) {
	// ModifiedStatus lists resources that differ from the bundle (real drift,
	// missing or extra objects). Restarting the agent does not fix those, so
	// leave such BundleDeployments alone.
	//
	// An empty NonReadyStatus means the agent reported no not-ready resources;
	// there is nothing to compare against the cluster, so we cannot tell
	// whether the status is stale.
	if len(bd.Status.ModifiedStatus) > 0 || len(bd.Status.NonReadyStatus) == 0 {
		return false, nil
	}

	// Find the Ready condition. Only Ready=False is a candidate: Ready=True
	// needs no fix, and Unknown or a missing condition means the agent has
	// not reported a result yet.
	var lastUpdate string
	notReady := false
	for _, c := range bd.Status.Conditions {
		if c.Type == "Ready" {
			notReady = c.Status == "False"
			lastUpdate = c.LastUpdateTime
			break
		}
	}
	if !notReady {
		return false, nil
	}

	// A working agent updates the status within seconds after a resource
	// becomes ready. Require the condition to stay unchanged for
	// staleStatusGrace, so we do not race an agent that is about to catch
	// up. An unparsable timestamp is treated as not stale.
	updatedAt, err := time.Parse(time.RFC3339, lastUpdate)
	if err != nil || time.Since(updatedAt) < staleStatusGrace {
		return false, nil
	}

	// The status is stale only if every resource the agent still lists as
	// not ready is in fact ready now. One resource that is really not ready,
	// or of a kind we cannot check, means the NotReady status may be correct.
	for _, s := range bd.Status.NonReadyStatus {
		ready, err := isWorkloadReady(ctx, r, s)
		if err != nil || !ready {
			return false, err
		}
	}
	return true, nil
}

// isWorkloadReady reports whether a resource listed as non-ready by fleet is
// actually ready. Only apps/v1 workloads are checked; anything else counts
// as not ready, so the workaround stays conservative.
func isWorkloadReady(ctx context.Context, r *resources.Resources, s fleetv1.NonReadyStatus) (bool, error) {
	if s.APIVersion != "" && s.APIVersion != "apps/v1" {
		return false, nil
	}

	var obj k8s.Object
	switch s.Kind {
	case "Deployment":
		obj = &appsv1.Deployment{}
	case "DaemonSet":
		obj = &appsv1.DaemonSet{}
	case "StatefulSet":
		obj = &appsv1.StatefulSet{}
	default:
		return false, nil
	}
	if err := r.Get(ctx, s.Name, s.Namespace, obj); err != nil {
		return false, err
	}

	switch o := obj.(type) {
	case *appsv1.Deployment:
		want := int32(1)
		if o.Spec.Replicas != nil {
			want = *o.Spec.Replicas
		}
		st := o.Status
		return st.ObservedGeneration >= o.Generation &&
			st.UpdatedReplicas == want && st.ReadyReplicas == want && st.AvailableReplicas == want, nil
	case *appsv1.DaemonSet:
		st := o.Status
		return st.ObservedGeneration >= o.Generation &&
			st.UpdatedNumberScheduled == st.DesiredNumberScheduled &&
			st.NumberReady == st.DesiredNumberScheduled &&
			st.NumberAvailable == st.DesiredNumberScheduled, nil
	case *appsv1.StatefulSet:
		want := int32(1)
		if o.Spec.Replicas != nil {
			want = *o.Spec.Replicas
		}
		st := o.Status
		return st.ObservedGeneration >= o.Generation &&
			st.UpdatedReplicas == want && st.ReadyReplicas == want, nil
	}
	return false, nil
}

// restartFleetAgent does the same as `kubectl rollout restart`.
func restartFleetAgent(ctx context.Context, r *resources.Resources) error {
	deployment := &appsv1.Deployment{}
	deployment.Name = fleetAgentName
	deployment.Namespace = fleetAgentNamespace

	patch := fmt.Sprintf(`{"spec":{"template":{"metadata":{"annotations":{"kubectl.kubernetes.io/restartedAt":%q}}}}}`,
		time.Now().Format(time.RFC3339))
	return r.Patch(ctx, deployment, k8s.Patch{PatchType: types.StrategicMergePatchType, Data: []byte(patch)})
}
