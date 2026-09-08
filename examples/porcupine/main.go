// Worked example: check a hokea run for per-key register linearizability
// using Porcupine, the standard Go linearizability checker. The key-value
// model is adapted from Porcupine's own examples
// (github.com/anishathalye/porcupine). Modify kvInput/kvOutput/kvModel and
// toOperations for your system's operations.
//
//	hokea export --format porcupine runs/<run>
//	cd examples/porcupine && go run . ../../runs/<run>/porcupine.json
//
// Go is needed only for this example, not for the rest of hokea.
package main

import (
	"encoding/json"
	"fmt"
	"math"
	"os"

	"github.com/anishathalye/porcupine"
)

type export struct {
	Format string       `json:"format"`
	Ops    []exportedOp `json:"ops"`
}

type exportedOp struct {
	Client   int            `json:"client"`
	Op       string         `json:"op"`
	Key      string         `json:"key"`
	Args     map[string]any `json:"args"`
	CallNs   int64          `json:"call_ns"`
	ReturnNs *int64         `json:"return_ns"` // null = still open
	Outcome  string         `json:"outcome"`
	Response map[string]any `json:"response"`
}

type kvInput struct {
	Op    string // "put" or "get"
	Key   string
	Value string
}

type kvOutput struct {
	Value string // value a get observed; "" means the key was absent
}

var kvModel = porcupine.Model{
	// Each key is an independent register; checking them separately keeps
	// the search small.
	Partition: func(history []porcupine.Operation) [][]porcupine.Operation {
		byKey := map[string][]porcupine.Operation{}
		for _, op := range history {
			key := op.Input.(kvInput).Key
			byKey[key] = append(byKey[key], op)
		}
		var parts [][]porcupine.Operation
		for _, ops := range byKey {
			parts = append(parts, ops)
		}
		return parts
	},
	Init: func() any { return "" }, // register starts absent
	Step: func(state, input, output any) (bool, any) {
		in := input.(kvInput)
		if in.Op == "put" {
			return true, in.Value
		}
		return output.(kvOutput).Value == state.(string), state
	},
	DescribeOperation: func(input, output any) string {
		in := input.(kvInput)
		if in.Op == "put" {
			return fmt.Sprintf("put(%s, %q)", in.Key, in.Value)
		}
		return fmt.Sprintf("get(%s) -> %q", in.Key, output.(kvOutput).Value)
	},
}

// toOperations applies the outcome rules — this is the part to read closely:
//   - rejected ops provably did not happen: dropped.
//   - unknown-outcome gets observed nothing and constrain nothing: dropped.
//   - unknown-outcome puts MAY have taken effect at any later moment: kept,
//     with a return time of "never" so the checker treats them as
//     concurrent with everything after their invocation.
//   - ok ops map directly.
func toOperations(exported []exportedOp) []porcupine.Operation {
	var ops []porcupine.Operation
	for _, e := range exported {
		if e.Outcome == "rejected" || (e.Outcome == "unknown" && e.Op == "get") {
			continue
		}
		value := ""
		if v, ok := e.Args["value"].(string); ok {
			value = v
		}
		observed := ""
		if v, ok := e.Response["value"].(string); ok {
			observed = v
		}
		ret := int64(math.MaxInt64)
		if e.ReturnNs != nil {
			ret = *e.ReturnNs
		}
		ops = append(ops, porcupine.Operation{
			ClientId: e.Client,
			Input:    kvInput{Op: e.Op, Key: e.Key, Value: value},
			Output:   kvOutput{Value: observed},
			Call:     e.CallNs,
			Return:   ret,
		})
	}
	return ops
}

func main() {
	if len(os.Args) != 2 {
		fmt.Fprintln(os.Stderr, "usage: go run . <porcupine.json>")
		os.Exit(2)
	}
	data, err := os.ReadFile(os.Args[1])
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(2)
	}
	var e export
	if err := json.Unmarshal(data, &e); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(2)
	}
	if e.Format != "hokea-porcupine-v1" {
		fmt.Fprintf(os.Stderr, "unexpected format %q\n", e.Format)
		os.Exit(2)
	}
	if porcupine.CheckOperations(kvModel, toOperations(e.Ops)) {
		fmt.Println("linearizable: true")
	} else {
		fmt.Println("linearizable: false")
		os.Exit(1)
	}
}
