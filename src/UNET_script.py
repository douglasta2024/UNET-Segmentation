import os
import torch
import nibabel as nb
from monai.networks.nets import UNet
from monai.data import ArrayDataset, DataLoader
from monai.metrics import DiceMetric
from monai.losses import DiceLoss
from monai.transforms import (
    Compose,
    RandSpatialCrop,
    LoadImage,
    ScaleIntensityRange,
    Orientation,
)
import ignite
from ignite.engine import Events, Engine
from ignite.metrics import Recall, Precision
import matplotlib.pyplot as plt
from matplotlib.animation import FuncAnimation

def generate_output():
    ### GLOBAL VARIABLES
    ROOT_PATH = os.getcwd()
    MODEL_PATH = os.path.join(ROOT_PATH, "models", "model.pth")
    DATA_PATH = os.path.join(ROOT_PATH, "data")
    DEVICE = torch.device("cpu")
    model_weights = torch.load(MODEL_PATH, weights_only=True, map_location=DEVICE)
    model = UNet(
        spatial_dims=3,
        in_channels=1,
        out_channels=1,
        channels=(16, 32, 64, 128, 256),
        strides=(2, 2, 2, 2),
        num_res_units=2,
    ).to(DEVICE)

    ### loading model weights onto model
    model.load_state_dict(model_weights)

    # list of paths to CT scans
    imgs = [os.path.join(DATA_PATH, "volume-20.nii"), os.path.join(DATA_PATH, "volume-25.nii")]
    segs = [os.path.join(DATA_PATH, "new-segmentation-20.nii"), os.path.join(DATA_PATH, "new-segmentation-25.nii")]

    # evaluation setup
    loss = DiceLoss()
    store_predictions = []
    mean_dice_metric = DiceMetric(include_background=True, reduction="mean")
    mean_dice_score = -1

    def evaluation_step(engine, batch):
        model.eval()
        with torch.no_grad():
            images, masks = batch
            images, masks = images.to(DEVICE), masks.to(DEVICE)
            outputs = model(images)

            logits = model(images)

            if logits.shape[1] == 1:  # Binary segmentation
                predictions = torch.sigmoid(logits) > 0.5  # Threshold at 0.5
            else:  # Multi-class segmentation
                predictions = torch.argmax(torch.softmax(logits, dim=1), dim=1)
            
            return predictions, masks        

    evaluator = Engine(evaluation_step)

    # Attach DiceMetric calculation at every iteration
    @evaluator.on(Events.ITERATION_COMPLETED)
    def update_dice_metric(engine):
        predictions, masks = engine.state.output
        mean_dice_metric(y_pred=predictions, y=masks)

    # Log results at the end of evaluation
    @evaluator.on(Events.COMPLETED)
    def log_mean_dice(engine):
        mean_dice_score = mean_dice_metric.aggregate().item()
        print(f"Mean Dice Score: {mean_dice_score:.4f}")
        #mean_dice_metric.reset()  # Reset metric for next run

    Recall(average=True).attach(evaluator, "recall")
    Precision(average=False).attach(evaluator, "precision")

    # Directory to save predictions
    output_dir = os.path.join(ROOT_PATH, "saved_gifs")
    os.makedirs(output_dir, exist_ok=True)

    @evaluator.on(Events.ITERATION_COMPLETED)
    def save_segmentation_masks(engine):
        predictions, ground_truth = engine.state.output  # Get predictions and masks from the step function
        
        # Save predicted masks
        for idx, prediction in enumerate(predictions):
            store_predictions.append([prediction*1, ground_truth[idx]])

    amin = -22.18
    amax = 450.0

    image_transforms = Compose(
        [
            LoadImage(image_only=True, ensure_channel_first=True),
            ScaleIntensityRange(
                a_min=amin,
                a_max=amax,
                b_min=0.0,
                b_max=1.0,
                clip=True,
            ),
            Orientation(axcodes="RAS"),   
            RandSpatialCrop(
            (512,512,160), 
                random_center=False
            ), 
        ]
    )

    seg_transforms = Compose(
        [
            LoadImage(image_only=True, ensure_channel_first=True),
            Orientation(axcodes="RAS"),
            RandSpatialCrop(
            (512,512,160), 
                random_center=False
            ), 
        ]
    )


    test_ds = ArrayDataset(img=imgs, img_transform=image_transforms, seg=segs, seg_transform=seg_transforms)
    test_loader = DataLoader(test_ds, batch_size=1, pin_memory=torch.cuda.is_available())

    print("Initiating testing...")
    evaluator.run(test_loader)

    mean_dice_score = mean_dice_metric.aggregate().item()
    recall = evaluator.state.metrics["recall"]
    precision = evaluator.state.metrics["precision"]
    print(f"Recall: {recall} | Precision {precision}")

    mask = store_predictions[0][0].squeeze().T
    ground_truth = store_predictions[0][1].squeeze().T
    mask2 = store_predictions[1][0].squeeze().T
    ground_truth2 = store_predictions[1][1].squeeze().T

    def gif_generator(image):
        fig, ax = plt.subplots()
        image_slice = ax.imshow(image[0].numpy(), cmap="bone", animated=True)

        # update function for each frame
        def update(frame):
            image_slice.set_array(image[frame].numpy())  # Update the image data
            ax.set_title(f"Slice {frame}")
            return [image_slice]

        # Create an animation
        ani = FuncAnimation(fig, update, frames=image.shape[0], interval=300, blit=True)    
        return ani

    print("Initiating GIF Generation")
    masks = [mask, ground_truth, mask2, ground_truth2]
    for idx, img in enumerate(masks):
        ani = gif_generator(img)
        ani.save(os.path.join(ROOT_PATH, f"saved_gifs/mask{idx}.gif"), writer="pillow")

    return mean_dice_score, recall, precision

