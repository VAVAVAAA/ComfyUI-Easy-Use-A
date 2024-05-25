import time, os, psutil
import comfy.utils
import comfy.sd
import comfy.controlnet
import folder_paths
from nodes import NODE_CLASS_MAPPINGS
from collections import defaultdict
from .log import log_node_info, log_node_error

stable_diffusion_loaders = ["easy fullLoader", "easy a1111Loader", "easy comfyLoader", "easy zero123Loader", "easy svdLoader"]
stable_cascade_loaders = ["easy cascadeLoader"]
controlnet_loaders = ["easy controlnetLoader", "easy controlnetLoaderADV"]
instant_loaders = ["easy instantIDApply", "easy instantIDApplyADV"]
cascade_vae_node = ["easy preSamplingCascade", "easy fullCascadeKSampler"]
model_merge_node = ["easy XYInputs: ModelMergeBlocks"]
lora_widget = ["easy fullLoader", "easy a1111Loader", "easy comfyLoader"]

class easyLoader:
    def __init__(self):
        self.loaded_objects = {
            "ckpt": defaultdict(tuple),  # {ckpt_name: (model, ...)}
            "unet": defaultdict(tuple),
            "clip": defaultdict(tuple),
            "clip_vision": defaultdict(tuple),
            "bvae": defaultdict(tuple),
            "vae": defaultdict(object),
            "lora": defaultdict(dict),  # {lora_name: {UID: (model_lora, clip_lora)}}
            "controlnet": defaultdict(dict),
        }
        self.memory_threshold = self.determine_memory_threshold(0.9)
        self.lora_name_cache = []

    def clean_values(self, values: str):
        original_values = values.split("; ")
        cleaned_values = []

        for value in original_values:
            cleaned_value = value.strip(';').strip()
            if cleaned_value == "":
                continue
            try:
                cleaned_value = int(cleaned_value)
            except ValueError:
                try:
                    cleaned_value = float(cleaned_value)
                except ValueError:
                    pass
            cleaned_values.append(cleaned_value)

        return cleaned_values

    def clear_unused_objects(self, desired_names: set, object_type: str):
        keys = set(self.loaded_objects[object_type].keys())
        for key in keys - desired_names:
            del self.loaded_objects[object_type][key]

    def get_input_value(self, entry, key, prompt=None):
        val = entry["inputs"][key]
        if isinstance(val, str):
            return val
        elif isinstance(val, list):
            if prompt is not None and val[0]:
                return prompt[val[0]]['inputs'][key]
            else:
                return val[0]
        else:
            return str(val)

    def process_pipe_loader(self, entry, desired_ckpt_names, desired_vae_names, desired_lora_names, desired_lora_settings, num_loras=3, suffix=""):
        for idx in range(1, num_loras + 1):
            lora_name_key = f"{suffix}lora{idx}_name"
            desired_lora_names.add(self.get_input_value(entry, lora_name_key))
            setting = f'{self.get_input_value(entry, lora_name_key)};{entry["inputs"][f"{suffix}lora{idx}_model_strength"]};{entry["inputs"][f"{suffix}lora{idx}_clip_strength"]}'
            desired_lora_settings.add(setting)

        desired_ckpt_names.add(self.get_input_value(entry, f"{suffix}ckpt_name"))
        desired_vae_names.add(self.get_input_value(entry, f"{suffix}vae_name"))

    def update_loaded_objects(self, prompt):
        desired_ckpt_names = set()
        desired_unet_names = set()
        desired_clip_names = set()
        desired_vae_names = set()
        desired_lora_names = set()
        desired_lora_settings = set()
        desired_controlnet_names = set()

        for entry in prompt.values():
            class_type = entry["class_type"]
            if class_type in lora_widget:
                lora_name = self.get_input_value(entry, "lora_name")
                desired_lora_names.add(lora_name)
                setting = f'{lora_name};{entry["inputs"]["lora_model_strength"]};{entry["inputs"]["lora_clip_strength"]}'
                desired_lora_settings.add(setting)

            if class_type in stable_diffusion_loaders:
                desired_ckpt_names.add(self.get_input_value(entry, "ckpt_name", prompt))
                desired_vae_names.add(self.get_input_value(entry, "vae_name"))

            elif class_type in stable_cascade_loaders:
                desired_unet_names.add(self.get_input_value(entry, "stage_c"))
                desired_unet_names.add(self.get_input_value(entry, "stage_b"))
                desired_clip_names.add(self.get_input_value(entry, "clip_name"))
                desired_vae_names.add(self.get_input_value(entry, "stage_a"))

            elif class_type in cascade_vae_node:
                encode_vae_name = self.get_input_value(entry, "encode_vae_name")
                decode_vae_name = self.get_input_value(entry, "decode_vae_name")
                if encode_vae_name and encode_vae_name != 'None':
                    desired_vae_names.add(encode_vae_name)
                if decode_vae_name and decode_vae_name != 'None':
                    desired_vae_names.add(decode_vae_name)

            elif class_type in controlnet_loaders:
                control_net_name = self.get_input_value(entry, "control_net_name", prompt)
                scale_soft_weights = self.get_input_value(entry, "scale_soft_weights")
                desired_controlnet_names.add(f'{control_net_name};{scale_soft_weights}')

            elif class_type in instant_loaders:
                control_net_name = self.get_input_value(entry, "control_net_name", prompt)
                scale_soft_weights = self.get_input_value(entry, "cn_soft_weights")
                desired_controlnet_names.add(f'{control_net_name};{scale_soft_weights}')

            elif class_type in model_merge_node:
                desired_ckpt_names.add(self.get_input_value(entry, "ckpt_name_1"))
                desired_ckpt_names.add(self.get_input_value(entry, "ckpt_name_2"))
                vae_use = self.get_input_value(entry, "vae_use")
                if vae_use != 'Use Model 1' and vae_use != 'Use Model 2':
                    desired_vae_names.add(vae_use)

        object_types = ["ckpt", "unet", "clip", "bvae", "vae", "lora", "controlnet"]
        for object_type in object_types:
            if object_type == 'unet':
                desired_names = desired_unet_names
            elif object_type in ["ckpt", "clip", "bvae"]:
                if object_type == 'clip':
                    desired_names = desired_ckpt_names.union(desired_clip_names)
                else:
                    desired_names = desired_ckpt_names
            elif object_type == "vae":
                desired_names = desired_vae_names
            elif object_type == "controlnet":
                desired_names = desired_controlnet_names
            else:
                desired_names = desired_lora_names
            self.clear_unused_objects(desired_names, object_type)

    def add_to_cache(self, obj_type, key, value):
        """
        Add an item to the cache with the current timestamp.
        """
        timestamped_value = (value, time.time())
        self.loaded_objects[obj_type][key] = timestamped_value

    def determine_memory_threshold(self, percentage=0.8):
        """
        Determines the memory threshold as a percentage of the total available memory.
        Args:
        - percentage (float): The fraction of total memory to use as the threshold.
                              Should be a value between 0 and 1. Default is 0.8 (80%).
        Returns:
        - memory_threshold (int): Memory threshold in bytes.
        """
        total_memory = psutil.virtual_memory().total
        memory_threshold = total_memory * percentage
        return memory_threshold

    def get_memory_usage(self):
        """
        Returns the memory usage of the current process in bytes.
        """
        process = psutil.Process(os.getpid())
        return process.memory_info().rss

    def eviction_based_on_memory(self):
        """
        Evicts objects from cache based on memory usage and priority.
        """
        current_memory = self.get_memory_usage()
        if current_memory < self.memory_threshold:
            return
        eviction_order = ["vae", "lora", "bvae", "clip", "ckpt", "controlnet"]
        for obj_type in eviction_order:
            if current_memory < self.memory_threshold:
                break
            # Sort items based on age (using the timestamp)
            items = list(self.loaded_objects[obj_type].items())
            items.sort(key=lambda x: x[1][1])  # Sorting by timestamp

            for item in items:
                if current_memory < self.memory_threshold:
                    break
                del self.loaded_objects[obj_type][item[0]]
                current_memory = self.get_memory_usage()

    def load_checkpoint(self, ckpt_name, config_name=None, load_vision=False):
        cache_name = ckpt_name
        if config_name not in [None, "Default"]:
            cache_name = ckpt_name + "_" + config_name
        if cache_name in self.loaded_objects["ckpt"]:
            clip_vision = self.loaded_objects["clip_vision"][cache_name][0] if load_vision else None
            clip = self.loaded_objects["clip"][cache_name][0] if not load_vision else None
            return self.loaded_objects["ckpt"][cache_name][0], clip, self.loaded_objects["bvae"][cache_name][0], clip_vision

        ckpt_path = folder_paths.get_full_path("checkpoints", ckpt_name)

        output_clip = False if load_vision else True
        output_clipvision = True if load_vision else False
        if config_name not in [None, "Default"]:
            config_path = folder_paths.get_full_path("configs", config_name)
            loaded_ckpt = comfy.sd.load_checkpoint(config_path, ckpt_path, output_vae=True, output_clip=output_clip, embedding_directory=folder_paths.get_folder_paths("embeddings"))
        else:
            loaded_ckpt = comfy.sd.load_checkpoint_guess_config(ckpt_path, output_vae=True, output_clip=output_clip, output_clipvision=output_clipvision, embedding_directory=folder_paths.get_folder_paths("embeddings"))

        self.add_to_cache("ckpt", cache_name, loaded_ckpt[0])
        self.add_to_cache("bvae", cache_name, loaded_ckpt[2])

        clip = loaded_ckpt[1]
        clip_vision = loaded_ckpt[3]
        if clip:
            self.add_to_cache("clip", cache_name, clip)
        if clip_vision:
            self.add_to_cache("clip_vision", cache_name, clip_vision)

        self.eviction_based_on_memory()

        return loaded_ckpt[0], clip, loaded_ckpt[2], clip_vision

    def load_vae(self, vae_name):
        if vae_name in self.loaded_objects["vae"]:
            return self.loaded_objects["vae"][vae_name][0]

        vae_path = folder_paths.get_full_path("vae", vae_name)
        sd = comfy.utils.load_torch_file(vae_path)
        loaded_vae = comfy.sd.VAE(sd=sd)
        self.add_to_cache("vae", vae_name, loaded_vae)
        self.eviction_based_on_memory()

        return loaded_vae

    def load_unet(self, unet_name):
        if unet_name in self.loaded_objects["unet"]:
            return self.loaded_objects["unet"][unet_name][0]

        unet_path = folder_paths.get_full_path("unet", unet_name)
        model = comfy.sd.load_unet(unet_path)
        self.add_to_cache("unet", unet_name, model)
        self.eviction_based_on_memory()

        return model

    def load_controlnet(self, control_net_name, scale_soft_weights=1):
        unique_id = f'{control_net_name};{str(scale_soft_weights)}'
        if unique_id in self.loaded_objects["controlnet"]:
            return self.loaded_objects["controlnet"][unique_id][0]
        if scale_soft_weights < 1:
            if "ScaledSoftControlNetWeights" in NODE_CLASS_MAPPINGS:
                soft_weight_cls = NODE_CLASS_MAPPINGS['ScaledSoftControlNetWeights']
                (weights, timestep_keyframe) = soft_weight_cls().load_weights(scale_soft_weights, False)
                cn_adv_cls = NODE_CLASS_MAPPINGS['ControlNetLoaderAdvanced']
                control_net, = cn_adv_cls().load_controlnet(control_net_name, timestep_keyframe)
            else:
                raise Exception(
                    f"[Advanced-ControlNet Not Found] you need to install 'COMFYUI-Advanced-ControlNet'")
        else:
            controlnet_path = folder_paths.get_full_path("controlnet", control_net_name)
            control_net = comfy.controlnet.load_controlnet(controlnet_path)
        self.add_to_cache("controlnet", unique_id, control_net)
        self.eviction_based_on_memory()
        return control_net
    def load_clip(self, clip_name, type='stable_diffusion', load_clip=None):
        if type == 'stable_diffusion':
            clip_type = comfy.sd.CLIPType.STABLE_DIFFUSION
        else:
            clip_type = comfy.sd.CLIPType.STABLE_CASCADE
        clip_path = folder_paths.get_full_path("clip", clip_name)
        load_clip = comfy.sd.load_clip(ckpt_paths=[clip_path], embedding_directory=folder_paths.get_folder_paths("embeddings"), clip_type=clip_type)
        self.add_to_cache("clip", clip_name, load_clip)
        self.eviction_based_on_memory()

        return load_clip

    def load_lora(self, lora, model=None, clip=None):
        lora_name = lora["lora_name"]
        model = model if model is not None else lora["model"]
        clip = clip if clip is not None else lora["clip"]
        model_strength = lora["model_strength"]
        clip_strength = lora["clip_strength"]
        lbw = lora["lbw"] if "lbw" in lora else None
        lbw_a = lora["lbw_a"] if "lbw_a" in lora else None
        lbw_b = lora["lbw_b"] if "lbw_b" in lora else None

        model_hash = str(model)[44:-1]
        clip_hash = str(clip)[25:-1]

        unique_id = f'{model_hash};{clip_hash};{lora_name};{model_strength};{clip_strength}'

        if unique_id in self.loaded_objects["lora"] and unique_id in self.loaded_objects["lora"][lora_name]:
            return self.loaded_objects["lora"][unique_id][0]

        orig_lora_name = lora_name
        lora_name = self.resolve_lora_name(lora_name)

        if lora_name is not None:
            lora_path = folder_paths.get_full_path("loras", lora_name)
        else:
            lora_path = None

        if lora_path is not None:
            log_node_info("Load LORA",f"{lora_name}: {model_strength}, {clip_strength}, LBW={lbw}, A={lbw_a}, B={lbw_b}")
            if lbw:
                lbw = lora["lbw"]
                lbw_a = lora["lbw_a"]
                lbw_b = lora["lbw_b"]
                if 'LoraLoaderBlockWeight //Inspire' not in NODE_CLASS_MAPPINGS:
                    raise Exception('[InspirePack Not Found] you need to install ComfyUI-Inspire-Pack')
                cls = NODE_CLASS_MAPPINGS['LoraLoaderBlockWeight //Inspire']
                model, clip, _ = cls().doit(model, clip, lora_name, model_strength, clip_strength, False, 0,
                                            lbw_a, lbw_b, "", lbw)
            else:
                _lora = comfy.utils.load_torch_file(lora_path, safe_load=True)
                keys = _lora.keys()
                if "down_blocks.0.resnets.0.norm1.bias" in keys:
                    print('Using LORA for Resadapter')
                    key_map = {}
                    key_map = comfy.lora.model_lora_keys_unet(model.model, key_map)
                    mapping_norm = {}

                    for key in keys:
                        if ".weight" in key:
                            key_name_in_ori_sd = key_map[key.replace(".weight", "")]
                            mapping_norm[key_name_in_ori_sd] = _lora[key]
                        elif ".bias" in key:
                            key_name_in_ori_sd = key_map[key.replace(".bias", "")]
                            mapping_norm[key_name_in_ori_sd.replace(".weight", ".bias")] = _lora[
                                key
                            ]
                        else:
                            print("===>Unexpected key", key)
                            mapping_norm[key] = _lora[key]

                    for k in mapping_norm.keys():
                        if k not in model.model.state_dict():
                            print("===>Missing key:", k)
                    model.model.load_state_dict(mapping_norm, strict=False)
                    return (model, clip)

                model, clip = comfy.sd.load_lora_for_models(model, clip, _lora, model_strength, clip_strength)

            self.add_to_cache("lora", unique_id, (model, clip))
            self.eviction_based_on_memory()
        else:
            log_node_error(f"LORA NOT FOUND", orig_lora_name)

        return model, clip

    def resolve_lora_name(self, name):
        if os.path.exists(name):
            return name
        else:
            if len(self.lora_name_cache) == 0:
                loras = folder_paths.get_filename_list("loras")
                self.lora_name_cache.extend(loras)
            for x in self.lora_name_cache:
                if x.endswith(name):
                    return x

            # 如果刷新网页后新添加的lora走这个逻辑
            log_node_info("LORA NOT IN CACHE", f"{name}")
            loras = folder_paths.get_filename_list("loras")
            for x in loras:
                if x.endswith(name):
                    self.lora_name_cache.append(x)
                    return x

            return None