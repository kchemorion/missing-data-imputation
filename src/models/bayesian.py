# src/models/bayesian.py

import numpy as np
from pgmpy.models import BayesianNetwork
from pgmpy.factors.discrete import TabularCPD
from pgmpy.inference import VariableElimination
from typing import Dict, List, Optional
import torch

class BayesianNetworkComponent:
    """Bayesian network component for capturing variable dependencies."""
    
    def __init__(self, n_variables: int, structure: Optional[List[tuple]] = None):
        self.n_variables = n_variables
        
        # Initialize network structure
        if structure is None:
            # Create default chain structure if none provided
            structure = [(f'V{i}', f'V{i+1}') for i in range(n_variables-1)]
        
        self.model = BayesianNetwork(structure)
        self.inference_engine = None
        self.cpds = {}
    
    def _initialize_cpds(self, data: np.ndarray):
        """Initialize conditional probability distributions from data."""
        # Discretize continuous data for initial CPDs
        discretized_data = self._discretize_data(data)
        
        for node in self.model.nodes():
            parents = self.model.get_parents(node)
            
            if not parents:  # Root node
                values = discretized_data[:, int(node[1])]
                probs = np.bincount(values, minlength=3) / len(values)
                # Reshape to (3, 1) as required by pgmpy
                cpd = TabularCPD(
                    variable=node,
                    variable_card=3,  # Using 3 discrete states
                    values=probs.reshape(3, 1)  # Changed this line
                )
            else:
                # Create CPD based on parent relationships
                parent_cards = [3] * len(parents)
                values = self._compute_conditional_probabilities(
                    discretized_data,
                    int(node[1]),
                    [int(p[1]) for p in parents]
                )
                
                # Reshape values to match pgmpy requirements
                values = values.T.reshape(3, -1)  # Added transpose and reshape
                
                cpd = TabularCPD(
                    variable=node,
                    variable_card=3,
                    values=values,
                    evidence=parents,
                    evidence_card=parent_cards
                )
            
            self.cpds[node] = cpd
            self.model.add_cpds(cpd)
            
        self.inference_engine = VariableElimination(self.model)
    
    def _discretize_data(self, data: np.ndarray, n_bins: int = 3) -> np.ndarray:
        """Discretize continuous data into bins."""
        discretized = np.zeros_like(data, dtype=int)
        
        for i in range(data.shape[1]):
            discretized[:, i] = np.digitize(
                data[:, i],
                bins=np.quantile(data[:, i], np.linspace(0, 1, n_bins+1)[1:-1])
            )
            
        return discretized
    
    def _compute_conditional_probabilities(
        self,
        data: np.ndarray,
        var_idx: int,
        parent_idx: List[int]
    ) -> np.ndarray:
        """Compute conditional probability tables from data."""
        var_data = data[:, var_idx]
        parent_data = data[:, parent_idx]
        
        # Create conditional probability table
        n_states = 3
        shape = [n_states] * (len(parent_idx) + 1)
        cpt = np.zeros(shape)
        
        # Count occurrences for each combination
        for i in range(len(data)):
            parent_state = tuple(parent_data[i])
            var_state = var_data[i]
            idx = tuple(list(parent_state) + [var_state])
            cpt[idx] += 1
            
        # Normalize
        cpt = cpt / (cpt.sum(axis=-1, keepdims=True) + 1e-10)
        
        return cpt.reshape(-1, n_states)
    
    def update_beliefs(
        self,
        latent_vectors: torch.Tensor,
        observed_data: Optional[torch.Tensor] = None
    ) -> Dict[str, np.ndarray]:
        """Update network beliefs based on new data."""
        latent_np = latent_vectors.detach().cpu().numpy()
        
        if self.inference_engine is None:
            self._initialize_cpds(latent_np)
            
        # Update CPDs with new data if provided
        if observed_data is not None:
            observed_np = observed_data.detach().cpu().numpy()
            self._update_cpds(observed_np)
            
        # Perform inference
        beliefs = {}
        for node in self.model.nodes():
            query_result = self.inference_engine.query([node])
            beliefs[node] = query_result.values
            
        return beliefs
    
    def _update_cpds(self, new_data: np.ndarray):
        """Update CPDs with new observations."""
        discretized_data = self._discretize_data(new_data)
        learning_rate = 0.1
        
        for node in self.model.nodes():
            parents = self.model.get_parents(node)
            old_cpd = self.cpds[node]
            
            if not parents:
                values = discretized_data[:, int(node[1])]
                new_values = np.bincount(values, minlength=3) / len(values)
                updated_values = (1 - learning_rate) * old_cpd.values.flatten() + learning_rate * new_values
                
                new_cpd = TabularCPD(
                    variable=node,
                    variable_card=3,
                    values=updated_values.reshape(3, 1)  # Reshape to (3, 1)
                )
            else:
                values = self._compute_conditional_probabilities(
                    discretized_data,
                    int(node[1]),
                    [int(p[1]) for p in parents]
                )
                updated_values = (1 - learning_rate) * old_cpd.values + learning_rate * values.T
                
                new_cpd = TabularCPD(
                    variable=node,
                    variable_card=3,
                    values=updated_values,
                    evidence=parents,
                    evidence_card=[3] * len(parents)
                )
            
            self.model.remove_cpds(old_cpd)
            self.model.add_cpds(new_cpd)
            self.cpds[node] = new_cpd
    def predict(self, input_data: torch.Tensor) -> torch.Tensor:
        """
        Make predictions using the Bayesian network.
        
        Args:
            input_data (torch.Tensor): Input data tensor [batch_size, n_features]
            
        Returns:
            torch.Tensor: Predicted values [batch_size, n_features]
        """
        # Convert input to numpy and discretize
        input_np = input_data.detach().cpu().numpy()
        discretized_input = self._discretize_data(input_np)
        
        # Initialize output array
        batch_size = input_np.shape[0]
        predictions = np.zeros_like(input_np)
        
        # Ensure CPDs are initialized
        if self.inference_engine is None:
            self._initialize_cpds(input_np)
        
        # Make predictions for each sample in the batch
        for i in range(batch_size):
            sample_evidence = {
                f'V{j}': discretized_input[i, j]
                for j in range(self.n_variables)
            }
            
            # Predict each variable
            for j in range(self.n_variables):
                var_name = f'V{j}'
                # Remove the current variable from evidence
                evidence = {k: v for k, v in sample_evidence.items() if k != var_name}
                
                # Query the network
                if evidence:  # Only query if we have evidence
                    query_result = self.inference_engine.query(
                        variables=[var_name],
                        evidence=evidence
                    )
                    # Get the most likely state
                    predicted_state = np.argmax(query_result.values)
                else:
                    # For variables without evidence, use prior probabilities
                    prior_probs = self.cpds[var_name].values.flatten()
                    predicted_state = np.argmax(prior_probs)
                
                # Convert discrete state back to continuous value
                # Use the middle value of each bin as the prediction
                predictions[i, j] = self._state_to_continuous(
                    predicted_state,
                    input_np[:, j]
                )
        
        # Convert back to torch tensor on the same device as input
        return torch.tensor(
            predictions,
            dtype=input_data.dtype,
            device=input_data.device
        )

    def _state_to_continuous(
        self,
        state: int,
        reference_values: np.ndarray,
        n_bins: int = 3
    ) -> float:
        """
        Convert discrete state back to continuous value.
        
        Args:
            state: Discrete state index
            reference_values: Reference values for the feature
            n_bins: Number of discretization bins
            
        Returns:
            float: Continuous value
        """
        # Calculate bin edges using the same method as in _discretize_data
        bin_edges = np.quantile(
            reference_values,
            np.linspace(0, 1, n_bins + 1)
        )
        
        # For the first and last states, use the bin edges
        if state == 0:
            return np.mean([bin_edges[0], bin_edges[1]])
        elif state == n_bins - 1:
            return np.mean([bin_edges[-2], bin_edges[-1]])
        else:
            # For middle states, use the average of adjacent bin edges
            return np.mean([bin_edges[state], bin_edges[state + 1]])