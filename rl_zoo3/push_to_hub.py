import argparse
import glob
import os
import shutil
import zipfile
from copy import deepcopy
from pathlib import Path
from pprint import pformat
from typing import Any, Optional

import torch as th
import yaml
from huggingface_hub import HfApi
from huggingface_hub.repocard import metadata_save
from huggingface_sb3 import EnvironmentName, ModelName, ModelRepoId
from fix_huggingface_sb3.push_to_hub import _evaluate_agent, _generate_replay, generate_metadata
from stable_baselines3.common.base_class import BaseAlgorithm
from stable_baselines3.common.utils import set_random_seed
from stable_baselines3.common.vec_env import VecEnv, unwrap_vec_normalize
from wasabi import Printer

import rl_zoo3.import_envs  # noqa: F401 pylint: disable=unused-import
from rl_zoo3 import ALGOS, get_saved_hyperparams
from rl_zoo3.exp_manager import ExperimentManager
from rl_zoo3.utils import StoreDict, create_test_env, get_model_path

msg = Printer()


def save_model_card(repo_dir: Path, generated_model_card: str, metadata: dict[str, Any]) -> None:
    """Saves a model card for the repository.

    :param repo_dir: repository directory
    :param generated_model_card: model card generated by _generate_model_card()
    :param metadata: metadata
    """
    readme_path = repo_dir / "README.md"
    # Always overwrite README
    with readme_path.open("w", encoding="utf-8") as f:
        f.write(generated_model_card)

    # Save our metrics to Readme metadata
    metadata_save(readme_path, metadata)


def generate_model_card(
    algo_name: str,
    algo_class_name: str,
    organization: str,
    env_id: str,
    mean_reward: float,
    std_reward: float,
    hyperparams: dict[str, Any],
    env_kwargs: dict[str, Any],
) -> tuple[str, dict[str, Any]]:
    """
    Generate the model card for the Hub

    :param algo_class_name: name of the algorithm class
    :param env_id: gym id of the environment
    :param mean_reward: mean reward of the agent
    :param std_reward: standard deviation of the mean reward of the agent
    :return: Model card (readme) and metadata (performance, algo/env id, tags)
    """
    # Step 1: Select the tags
    metadata = generate_metadata(algo_class_name, env_id, mean_reward, std_reward)

    # Step 2: Generate the model card
    model_card = f"""
# **{algo_class_name}** Agent playing **{env_id}**
This is a trained model of a **{algo_class_name}** agent playing **{env_id}**
using the [stable-baselines3 library](https://github.com/DLR-RM/stable-baselines3)
and the [RL Zoo](https://github.com/DLR-RM/rl-baselines3-zoo).

The RL Zoo is a training framework for Stable Baselines3
reinforcement learning agents,
with hyperparameter optimization and pre-trained agents included.
"""

    model_card += f"""
## Usage (with SB3 RL Zoo)

RL Zoo: https://github.com/DLR-RM/rl-baselines3-zoo<br/>
SB3: https://github.com/DLR-RM/stable-baselines3<br/>
SB3 Contrib: https://github.com/Stable-Baselines-Team/stable-baselines3-contrib
SBX (SB3 + Jax): https://github.com/araffin/sbx

Install the RL Zoo (with SB3 and SB3-Contrib):
```bash
pip install rl_zoo3
```

```
# Download model and save it into the logs/ folder
python -m rl_zoo3.load_from_hub --algo {algo_name} --env {env_id} -orga {organization} -f logs/
python -m rl_zoo3.enjoy --algo {algo_name} --env {env_id}  -f logs/
```

If you installed the RL Zoo3 via pip (`pip install rl_zoo3`), from anywhere you can do:
```
python -m rl_zoo3.load_from_hub --algo {algo_name} --env {env_id} -orga {organization} -f logs/
python -m rl_zoo3.enjoy --algo {algo_name} --env {env_id}  -f logs/
```

## Training (with the RL Zoo)
```
python -m rl_zoo3.train --algo {algo_name} --env {env_id} -f logs/
# Upload the model and generate video (when possible)
python -m rl_zoo3.push_to_hub --algo {algo_name} --env {env_id} -f logs/ -orga {organization}
```

## Hyperparameters
```python
{pformat(hyperparams)}
```
"""
    if len(env_kwargs) > 0:
        model_card += f"""
# Environment Arguments
```python
{pformat(env_kwargs)}
```
"""

    return model_card, metadata


def package_to_hub(
    model: BaseAlgorithm,
    model_name: ModelName,
    algo_name: str,
    algo_class_name: str,
    log_path: Path,
    hyperparams: dict[str, Any],
    env_kwargs: dict[str, Any],
    env_name: EnvironmentName,
    eval_env: VecEnv,
    repo_id: ModelRepoId,
    commit_message: str,
    is_deterministic: bool = True,
    n_eval_episodes=10,
    token: Optional[str] = None,
    local_repo_path="hub",
    video_length=1000,
    generate_video: bool = False,
):
    """
    Evaluate, Generate a video and Upload a model to Hugging Face Hub.
    This method does the complete pipeline:
    - It evaluates the model
    - It generates the model card
    - It generates a replay video of the agent
    - It pushes everything to the hub

    This is a work in progress function, if it does not work,
    use `push_to_hub` method.

    :param model: trained model
    :param model_name: name of the model
    :param algo_name: alias used in the zoo for the algorithm,
        usually lower case of the class (a2c, ars, ppo, ppo_lstm)
    :param algo_class_name: name of the architecture of your model
        Name of the algorithm class.
        (DQN, PPO, A2C, SAC, RecurrentPPO, ...)
    :param log_path: Path to where the model is saved in the zoo.
    :param hyperparams: Hyperparameters used for training,
        includes wrappers.
    :param env_kwargs: Additional keyword arguments that were passed
        to the environment.
    :param env_name: name of the environment
    :param eval_env: environment used to evaluate the agent
    :param repo_id: id of the model repository from the Hugging Face Hub
    :param commit_message: commit message
    :param is_deterministic: use deterministic or stochastic actions (by default: True)
    :param n_eval_episodes: number of evaluation episodes (by default: 10)
    :param local_repo_path: local repository path
    :param video_length: length of the video (in timesteps)
    """

    msg.info(
        "This function will save, evaluate, generate a video of your agent, "
        "create a model card and push everything to the hub. "
        "It might take up to some minutes if video generation is activated. "
        "This is a work in progress: if you encounter a bug, please open an issue."
    )

    organization, repo_name = repo_id.split("/")

    # Step 1: Clone or create the repo
    # Create the repo (or clone its content if it's nonempty)
    api = HfApi()

    repo_url = api.create_repo(
        token=token,
        repo_id=repo_id,
        private=False,
        exist_ok=True,
    )
    # Retrieve current repo state
    repo_local_path = Path(local_repo_path) / repo_name
    api.snapshot_download(repo_id=repo_id, local_dir=repo_local_path)

    # Add mp4 files to .gitattributes
    with open(repo_local_path / ".gitattributes", "a+") as f:
        f.seek(0)  # Move the file pointer to the beginning of the file
        if not any("*.mp4" in line for line in f):
            f.write("*.mp4 filter=lfs diff=lfs merge=lfs -text\n")

    # Step 1: Save the model
    print("Saving model to:", repo_local_path / model_name)
    model.save(repo_local_path / model_name)

    # Retrieve VecNormalize wrapper if it exists
    # we need to save the statistics
    maybe_vec_normalize = unwrap_vec_normalize(eval_env)

    # Save the normalization
    if maybe_vec_normalize is not None:
        maybe_vec_normalize.save(repo_local_path / "vec_normalize.pkl")
        # Do not update the stats at test time
        maybe_vec_normalize.training = False
        # Reward normalization is not needed at test time
        maybe_vec_normalize.norm_reward = False

    # Unzip the model
    with zipfile.ZipFile(repo_local_path / model_name.filename, "r") as zip_ref:
        zip_ref.extractall(repo_local_path / model_name)

    # Step 2: Copy config files
    args_path = log_path / env_name / "args.yml"
    config_path = log_path / env_name / "config.yml"

    shutil.copy(args_path, repo_local_path / "args.yml")
    shutil.copy(config_path, repo_local_path / "config.yml")
    with open(repo_local_path / "env_kwargs.yml", "w") as outfile:
        yaml.dump(env_kwargs, outfile)

    # Copy train/eval metrics into zip
    with zipfile.ZipFile(repo_local_path / "train_eval_metrics.zip", "w") as archive:
        if os.path.isfile(log_path / "evaluations.npz"):
            archive.write(log_path / "evaluations.npz", arcname="evaluations.npz")
        for monitor_file in glob.glob(f"{log_path}/*.csv"):
            archive.write(monitor_file, arcname=monitor_file.split(os.sep)[-1])

    # Step 3: Evaluate the agent
    mean_reward, std_reward = _evaluate_agent(model, eval_env, n_eval_episodes, is_deterministic, repo_local_path)

    # Step 4: Generate a video
    if generate_video:
        _generate_replay(model, eval_env, video_length, is_deterministic, repo_local_path)
        # Cleanup files after generation
        # TODO: upstream to huggingface sb3
        video_path = Path("test.mp4")
        if video_path.is_file():
            video_path.unlink()
        json_path = list(glob.glob("*.meta.json"))
        if len(json_path) > 0:
            Path(json_path[0]).unlink()

    # Step 5: Generate the model card
    generated_model_card, metadata = generate_model_card(
        algo_name,
        algo_class_name,
        organization,
        env_name.gym_id,
        mean_reward,
        std_reward,
        hyperparams,
        env_kwargs,
    )

    save_model_card(repo_local_path, generated_model_card, metadata)

    msg.info(f"Pushing repo {repo_name} to the Hugging Face Hub")
    api.upload_folder(repo_id=repo_id, folder_path=repo_local_path, commit_message=commit_message)

    msg.info(f"Your model is pushed to the hub. You can view your model here: {repo_url}")
    return repo_url


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--env", help="Environment ID", type=EnvironmentName, required=True)
    parser.add_argument("-f", "--folder", help="Log folder", type=str, required=True)
    parser.add_argument("--algo", help="RL Algorithm", type=str, required=True, choices=list(ALGOS.keys()))
    parser.add_argument("-n", "--n-timesteps", help="Number of timesteps for the video recording", default=1000, type=int)
    parser.add_argument("--num-threads", help="Number of threads for PyTorch (-1 to use default)", default=-1, type=int)
    parser.add_argument("--n-envs", help="Number of environments", default=1, type=int)
    parser.add_argument("--exp-id", help="Experiment ID (default: 0: latest, -1: no exp folder)", default=0, type=int)
    parser.add_argument("--verbose", help="Verbose mode (0: no output, 1: INFO)", default=1, type=int)
    parser.add_argument(
        "--no-render", action="store_true", default=False, help="Do not render the environment (useful for tests)"
    )
    parser.add_argument("--deterministic", action="store_true", default=False, help="Use deterministic actions")
    parser.add_argument("--device", help="PyTorch device to be use (ex: cpu, cuda...)", default="auto", type=str)
    parser.add_argument(
        "--load-best", action="store_true", default=False, help="Load best model instead of last model if available"
    )
    parser.add_argument(
        "--load-checkpoint",
        type=int,
        help="Load checkpoint instead of last model if available, "
        "you must pass the number of timesteps corresponding to it",
    )
    parser.add_argument(
        "--load-last-checkpoint",
        action="store_true",
        default=False,
        help="Load last checkpoint instead of last model if available",
    )
    parser.add_argument("--stochastic", action="store_true", default=False, help="Use stochastic actions")
    parser.add_argument("--seed", help="Random generator seed", type=int, default=0)
    parser.add_argument(
        "--env-kwargs", type=str, nargs="+", action=StoreDict, help="Optional keyword argument to pass to the env constructor"
    )
    parser.add_argument("-orga", "--organization", help="Huggingface hub organization", type=str, required=True)
    parser.add_argument("-name", "--repo-name", help="Huggingface hub repository name, by default 'algo-env'", type=str)
    parser.add_argument("-m", "--commit-message", help="Commit message", default="Initial commit", type=str)

    args = parser.parse_args()
    env_name: EnvironmentName = args.env
    algo = args.algo

    _, model_path, log_path = get_model_path(
        args.exp_id,
        args.folder,
        args.algo,
        args.env,
        args.load_best,
        args.load_checkpoint,
        args.load_last_checkpoint,
    )

    print(f"Loading {model_path}")

    # Off-policy algorithm only support one env for now
    off_policy_algos = ["qrdqn", "dqn", "ddpg", "sac", "her", "td3", "tqc"]

    if algo in off_policy_algos:
        args.n_envs = 1

    set_random_seed(args.seed)

    if args.num_threads > 0:
        if args.verbose > 1:
            print(f"Setting torch.num_threads to {args.num_threads}")
        th.set_num_threads(args.num_threads)

    is_atari = ExperimentManager.is_atari(env_name.gym_id)
    is_minigrid = ExperimentManager.is_minigrid(env_name.gym_id)

    stats_path = os.path.join(log_path, env_name)
    hyperparams, maybe_stats_path = get_saved_hyperparams(stats_path, test_mode=True)

    # load env_kwargs if existing
    env_kwargs = {}
    args_path = os.path.join(log_path, env_name, "args.yml")
    if os.path.isfile(args_path):
        with open(args_path) as f:
            loaded_args = yaml.load(f, Loader=yaml.UnsafeLoader)
            if loaded_args["env_kwargs"] is not None:
                env_kwargs = loaded_args["env_kwargs"]

    # render and record video by default
    should_render = not args.no_render
    if should_render:
        env_kwargs.update(render_mode="rgb_array")

    # overwrite with command line arguments
    if args.env_kwargs is not None:
        env_kwargs.update(args.env_kwargs)

    eval_env = create_test_env(
        env_name.gym_id,
        n_envs=args.n_envs,
        stats_path=maybe_stats_path,
        seed=args.seed,
        log_dir=None,
        should_render=should_render,
        hyperparams=deepcopy(hyperparams),
        env_kwargs=env_kwargs,
    )

    kwargs = dict(seed=args.seed)
    if algo in off_policy_algos:
        # Dummy buffer size as we don't need memory to enjoy the trained agent
        kwargs.update(dict(buffer_size=1))

        # Hack due to breaking change in v1.6
        # handle_timeout_termination cannot be at the same time
        # with optimize_memory_usage
        if "optimize_memory_usage" in hyperparams:
            kwargs.update(optimize_memory_usage=False)

    # Note: we assume that we push models using the same machine (same python version)
    # that trained them, if not, we would need to pass custom object as in enjoy.py
    custom_objects: dict[str, Any] = {}
    model = ALGOS[algo].load(model_path, env=eval_env, custom_objects=custom_objects, device=args.device, **kwargs)

    # Deterministic by default except for atari games
    stochastic = args.stochastic or ((is_atari or is_minigrid) and not args.deterministic)
    deterministic = not stochastic

    # Default model name, the model will be saved under "{algo}-{env_name}.zip"
    model_name = ModelName(algo, env_name)

    if args.repo_name is None:
        args.repo_name = model_name

    repo_id = ModelRepoId(args.organization, args.repo_name)
    print(f"Uploading to {repo_id}, make sure to have the rights")

    package_to_hub(
        model,
        model_name,
        algo,
        ALGOS[algo].__name__,
        Path(log_path),
        hyperparams,
        env_kwargs,
        env_name,
        eval_env,
        repo_id=repo_id,
        commit_message=args.commit_message,
        is_deterministic=deterministic,
        n_eval_episodes=10,
        token=None,
        local_repo_path="hub",
        video_length=args.n_timesteps,
        generate_video=not args.no_render,
    )
